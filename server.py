import os
import json
import secrets
from contextlib import asynccontextmanager
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
