import os
import re
import json
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

import boto3
import botocore.exceptions
import aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import chat_db

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

_HERE  = Path(__file__).parent
_LG_DB = _HERE / "langgraph.db"
_LG_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def _fetch_account_context() -> dict:
    try:
        identity = boto3.client("sts").get_caller_identity()
        return {
            "account_id": identity["Account"],
            "region": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            "arn": identity["Arn"],
        }
    except Exception:
        return {"account_id": "unknown", "region": os.environ.get("AWS_DEFAULT_REGION", ""), "arn": ""}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ctx = _fetch_account_context()
    os.environ["AWS_ACCOUNT_ID"] = ctx["account_id"]

    # WAL mode lets the delete endpoints open short-lived connections
    # alongside the long-lived AsyncSqliteSaver connection.
    async with aiosqlite.connect(str(_LG_DB)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.commit()

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from agents.chat_agent import build_chat_agent
    async with AsyncSqliteSaver.from_conn_string(str(_LG_DB)) as saver:
        app.state.agent = build_chat_agent(saver, ctx)
        yield


app = FastAPI(title="AWS Cost Chat", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "default"


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_HERE / "static" / "index.html").read_text()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    try:
        identity = boto3.client(
            "sts", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        ).get_caller_identity()
        return {
            "ok": True,
            "account": identity["Account"],
            "arn": identity["Arn"],
            "region": os.environ.get("AWS_DEFAULT_REGION", ""),
        }
    except botocore.exceptions.ClientError as e:
        return {"ok": False, "code": e.response["Error"]["Code"], "error": e.response["Error"]["Message"]}
    except Exception as e:
        return {"ok": False, "code": "UnknownError", "error": str(e)}


@app.get("/sessions")
async def list_sessions():
    return chat_db.get_sessions()


@app.get("/sessions/{thread_id}/messages")
async def get_session_messages(thread_id: str):
    return chat_db.get_messages(thread_id)


async def _delete_lg_thread(thread_id: str):
    try:
        async with aiosqlite.connect(str(_LG_DB)) as conn:
            for tbl in _LG_TABLES:
                try:
                    await conn.execute(f"DELETE FROM {tbl} WHERE thread_id = ?", (thread_id,))
                except Exception:
                    pass
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception:
        pass


async def _delete_lg_all():
    try:
        async with aiosqlite.connect(str(_LG_DB)) as conn:
            for tbl in _LG_TABLES:
                try:
                    await conn.execute(f"DELETE FROM {tbl}")
                except Exception:
                    pass
            try:
                await conn.commit()
            except Exception:
                pass
    except Exception:
        pass


@app.delete("/sessions/{thread_id}")
async def delete_session(thread_id: str):
    chat_db.delete_session(thread_id)
    await _delete_lg_thread(thread_id)
    return {"ok": True}


@app.delete("/sessions")
async def delete_all_sessions():
    chat_db.delete_all_sessions()
    await _delete_lg_all()
    return {"ok": True}


@app.post("/chat")
async def chat(req: ChatRequest):
    async def event_stream():
        title = req.message[:60] + ("…" if len(req.message) > 60 else "")
        chat_db.upsert_session(req.thread_id, title)
        chat_db.save_message(req.thread_id, "user", req.message)

        config = {"configurable": {"thread_id": req.thread_id}}
        assistant_parts: list[str] = []

        try:
            async for chunk, _metadata in app.state.agent.astream(
                {"messages": [{"role": "user", "content": req.message}]},
                config=config,
                stream_mode="messages",
            ):
                ctype = type(chunk).__name__

                if ctype == "AIMessageChunk":
                    text = _extract_text(chunk.content)
                    if text:
                        assistant_parts.append(text)
                        yield _sse("text", text)

                elif ctype == "AIMessage":
                    for tc in getattr(chunk, "tool_calls", None) or []:
                        if tc.get("name"):
                            yield _sse("tool_start", tc["name"])

                elif ctype == "ToolMessage":
                    name = getattr(chunk, "name", "") or ""
                    if name:
                        yield _sse("tool_end", name)

            if assistant_parts:
                chat_db.save_message(req.thread_id, "assistant", "".join(assistant_parts))
            chat_db.touch_session(req.thread_id)
            yield _sse("done", "")

        except Exception as exc:
            err = str(exc)
            if "ToolMessage" in err and "tool_calls" in err:
                yield _sse("session_corrupted", "thread-" + secrets.token_hex(6))
            else:
                yield _sse("error", err[:500])

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not isinstance(block, dict) or block.get("type") == "text"
        )
    return ""


def _sse(event_type: str, data: str) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data})}\n\n"


# ── Cost Optimization Analysis ────────────────────────────────────────────────

_ANALYSIS: dict = {
    "status": "idle",   # idle | running | done | error
    "result": None,
    "updated_at": None,
    "logs": [],
}


def _build_analysis_prompt() -> str:
    today_obj = date.today()
    today  = today_obj.isoformat()
    year_i, month_i = today_obj.year, today_obj.month
    month  = f"{year_i}-{month_i:02d}"
    start  = f"{month}-01"

    # Previous month bounds (CE end is exclusive, so "{month}-01" covers all of prev month)
    prev_year_i  = year_i if month_i > 1 else year_i - 1
    prev_month_i = month_i - 1 if month_i > 1 else 12
    prev_month   = f"{prev_year_i}-{prev_month_i:02d}"
    prev_start   = f"{prev_month}-01"

    return (
        f"Analyze this AWS account's costs and resources for {month}. Today: {today}.\n\n"
        "Follow these steps in order:\n"
        f"1. Current month costs by SERVICE: call_aws_api(\"ce\", \"get_cost_and_usage\") "
        f"— TimePeriod Start={start} End={today}, Granularity=MONTHLY, GroupBy SERVICE\n"
        f"2. Previous month costs by SERVICE: call_aws_api(\"ce\", \"get_cost_and_usage\") "
        f"— TimePeriod Start={prev_start} End={start}, Granularity=MONTHLY, GroupBy SERVICE\n"
        "3. call_aws_api(\"ec2\", \"describe_instances\")\n"
        "4. call_aws_api(\"ec2\", \"describe_volumes\")\n"
        "5. call_aws_api(\"rds\", \"describe_db_instances\")\n"
        "6. call_aws_api(\"elbv2\", \"describe_load_balancers\")\n"
        "7. call_aws_api(\"ce\", \"get_savings_plans_purchase_recommendation\")\n\n"
        "Then call execute_python with code that prints() a single JSON — no other text.\n\n"
        "In execute_python, compute:\n"
        "  import calendar; days_in_month = calendar.monthrange(year, month)[1]\n"
        "  is_partial = today.day < days_in_month\n"
        "  projected_end_usd = mtd_spend / today.day * days_in_month  (if is_partial)\n\n"
        "Required JSON shape:\n"
        "{\n"
        f'  "period": "{month}",\n'
        '  "is_partial": <bool>,\n'
        '  "total_monthly_spend_usd": <float, MTD spend>,\n'
        '  "projected_end_usd": <float, projected full-month total>,\n'
        f'  "previous_month_period": "{prev_month}",\n'
        '  "previous_month_usd": <float, previous month total>,\n'
        '  "potential_monthly_savings_usd": <float>,\n'
        '  "spend_by_service": [{"service": "Amazon EC2", "spend_usd": 0.0}],\n'
        '  "recommendations": [\n'
        '    {\n'
        '      "category": "EC2|EBS|RDS|S3|Network|Savings",\n'
        '      "severity": "high|medium|low",\n'
        '      "title": "...",\n'
        '      "description": "...",\n'
        '      "estimated_monthly_savings_usd": 0.0,\n'
        '      "resource_count": 0,\n'
        '      "action": "..."\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        "Severity: high>$100/mo, medium=$20-100, low<$20.\n"
        "Only include real findings. execute_python must be your FINAL action. Print ONLY the JSON."
    )


def _parse_analysis_json(text: str) -> dict | None:
    if not text:
        return None
    stripped = text.strip()
    # Direct JSON parse
    try:
        d = json.loads(stripped)
        if isinstance(d, dict) and "period" in d:
            return d
    except Exception:
        pass
    # Code fence: ```json ... ```
    m = re.search(r'```(?:json)?\s*(\{.+?\})\s*```', stripped, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and "period" in d:
                return d
        except Exception:
            pass
    # Bare JSON block ending the string
    m = re.search(r'(\{[^`]+?"period"[^`]+?\})\s*$', stripped, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and "period" in d:
                return d
        except Exception:
            pass
    return None


@app.get("/analysis")
async def get_analysis():
    return {
        "status": _ANALYSIS["status"],
        "result": _ANALYSIS["result"],
        "updated_at": _ANALYSIS["updated_at"],
        "logs": _ANALYSIS["logs"][-30:],
    }


@app.post("/analysis/run")
async def run_analysis():
    if _ANALYSIS["status"] == "running":
        async def _already():
            yield _sse("status", "running")
            yield _sse("done", "")
        return StreamingResponse(_already(), media_type="text/event-stream")

    async def stream():
        _ANALYSIS["status"] = "running"
        _ANALYSIS["logs"] = []
        _ANALYSIS["result"] = None
        yield _sse("status", "running")

        prompt = _build_analysis_prompt()
        thread_id = "analysis-" + secrets.token_hex(4)
        config = {"configurable": {"thread_id": thread_id}}
        text_parts: list[str] = []
        py_outputs: list[str] = []

        try:
            async for chunk, _meta in app.state.agent.astream(
                {"messages": [{"role": "user", "content": prompt}]},
                config=config,
                stream_mode="messages",
            ):
                ctype = type(chunk).__name__

                if ctype == "AIMessageChunk":
                    text_parts.append(_extract_text(chunk.content))

                elif ctype == "AIMessage":
                    for tc in getattr(chunk, "tool_calls", None) or []:
                        name = tc.get("name", "")
                        if name in ("call_aws_api", "execute_python"):
                            args = tc.get("args", {})
                            log = (
                                f"{args.get('service','')}.{args.get('operation','')}"
                                if name == "call_aws_api" else "execute_python"
                            )
                            _ANALYSIS["logs"].append(log)
                            yield _sse("log", log)

                elif ctype == "ToolMessage":
                    name = getattr(chunk, "name", "") or ""
                    content = getattr(chunk, "content", "") or ""
                    if name == "execute_python" and content:
                        py_outputs.append(str(content))
                    yield _sse("tool_end", name)

            # Prefer execute_python outputs (most recent first), fallback to full text
            result = None
            for out in reversed(py_outputs):
                result = _parse_analysis_json(out)
                if result:
                    break
            if not result:
                result = _parse_analysis_json("".join(text_parts))

            if result:
                _ANALYSIS.update({
                    "status": "done",
                    "result": result,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                yield _sse("result", json.dumps(result))
            else:
                _ANALYSIS["status"] = "error"
                yield _sse("error", "Could not extract structured result from agent output")

        except Exception as exc:
            _ANALYSIS["status"] = "error"
            yield _sse("error", str(exc)[:300])

        yield _sse("done", "")

    return StreamingResponse(stream(), media_type="text/event-stream")
