import os
import sys
import re
import argparse
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule

load_dotenv()

# ── CLI args (parsed before anything else so DATA_MODE is set before tool imports) ──
_parser = argparse.ArgumentParser(description="AWS Cloud Cost Optimization Advisor")
_mode_group = _parser.add_mutually_exclusive_group()
_mode_group.add_argument("--live", action="store_true", help="Use real AWS API data")
_mode_group.add_argument("--mock", action="store_true", help="Use mock data (default)")
_parser.add_argument("--debug", action="store_true", help="Print raw stream chunk types for debugging")
_args = _parser.parse_args()

if _args.live:
    os.environ["AWS_DATA_MODE"] = "live"
else:
    os.environ["AWS_DATA_MODE"] = "mock"  # default

DATA_MODE = os.environ["AWS_DATA_MODE"]

# Create timestamped run directory and expose it to report_tools via env var
_RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_DIR = Path("output") / _RUN_TIMESTAMP
_RUN_DIR.mkdir(parents=True, exist_ok=True)
os.environ["REPORT_OUTPUT_DIR"] = str(_RUN_DIR)
DEBUG = _args.debug

console = Console()

USER_PROMPT = """Analyze this AWS account for all cost optimization opportunities.

Run each specialist agent in order (EC2 → RDS → EBS → S3 → Network → Savings Plans → Lambda → CloudWatch), collect their findings, then have the report agent synthesize everything into a prioritized Markdown report.

Have the report agent save the JSON and HTML reports, then print a brief executive summary when done."""

AGENT_LABELS = {
    "ec2-analyst":        ("EC2", "Instances · Elastic IPs"),
    "ebs-analyst":        ("EBS", "Volumes · Snapshots · AMIs"),
    "rds-analyst":        ("RDS", "Databases · Reserved Instances"),
    "s3-analyst":         ("S3", "Buckets · Lifecycle · Replication"),
    "network-analyst":    ("Network", "Load Balancers · NAT · Data Transfer"),
    "savings-analyst":    ("Savings", "Savings Plans · Reserved Instances"),
    "lambda-analyst":     ("Lambda", "Functions · Memory · Runtimes"),
    "cloudwatch-analyst": ("CloudWatch", "Log Groups · Metrics · Alarms"),
    "report-agent":       ("Report", "Synthesizing all findings"),
}

# DeepAgents internal tools — suppress from user-facing output
INTERNAL_TOOLS = {"write_todos", "write_file", "read_file", "edit_file", "ls", "glob", "grep"}


def check_api_key():
    missing = [v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if not os.environ.get(v)]
    if missing:
        console.print("[bold red]Error:[/bold red] Missing AWS credentials: " + ", ".join(missing))
        console.print("Set them with:")
        console.print("  [cyan]export AWS_ACCESS_KEY_ID=your-key-id[/cyan]")
        console.print("  [cyan]export AWS_SECRET_ACCESS_KEY=your-secret-key[/cyan]")
        console.print("  [cyan]export AWS_DEFAULT_REGION=us-east-1[/cyan]")
        sys.exit(1)


def _extract_subagent_name(args: dict) -> str:
    for key in ("subagent_type", "subagent_name", "agent_name", "agent", "name"):
        if key in args:
            return args[key]
    return str(list(args.values())[0])[:40] if args else "subagent"


def _print_agent_start(name: str):
    label, detail = AGENT_LABELS.get(name, (name, ""))
    console.print()
    console.print(f"[bold cyan]▶ [{label}][/bold cyan] [yellow]{detail}[/yellow]")


def _print_agent_done(name: str):
    label, _ = AGENT_LABELS.get(name, (name, ""))
    console.print(f"  [green]✓ [{label}] complete[/green]")


def _print_tool_call(tool_name: str):
    console.print(f"  [dim]  ⚙ {tool_name}()[/dim]")


def _friendly_error(exc: Exception) -> str:
    """Convert common exceptions into human-readable messages."""
    msg = str(exc)
    cls = type(exc).__name__

    # botocore / boto3 errors
    if "ThrottlingException" in msg or "TooManyRequestsException" in msg:
        return "Bedrock is throttling requests. Wait 60 seconds and retry."
    if "ExpiredTokenException" in msg or "ExpiredToken" in msg:
        return "AWS credentials have expired. Renew your session token and retry."
    if "UnrecognizedClientException" in msg or "InvalidClientTokenId" in msg:
        return "AWS credentials are invalid. Check AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
    if "AccessDeniedException" in msg or "AccessDenied" in msg:
        return "Access denied. Check IAM permissions for Bedrock and the services you're querying."
    if "ResourceNotFoundException" in msg:
        return "Model not found on Bedrock. Check that Claude Sonnet 4.6 access is enabled in the AWS Console under Amazon Bedrock → Model access."
    if "ValidationException" in msg:
        return f"Bedrock rejected the request: {msg[:200]}"
    if "ConnectTimeoutError" in cls or "ReadTimeoutError" in cls or "ConnectionError" in cls:
        return "Network connection timed out. Check your internet connection and try again."
    if "ModelStreamErrorException" in msg:
        return "Bedrock stream error. The model may have returned an unexpected response — retry."

    return f"{cls}: {msg[:300]}"


def _extract_text(content) -> str:
    """Extract plain text from AIMessage content (string or Bedrock block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not isinstance(block, dict) or block.get("type") == "text"
        )
    return ""


def _save_partial(parts: list) -> None:
    """Save whatever text was collected before the error."""
    text = "".join(parts).strip()
    if not text:
        return
    output_path = _RUN_DIR / "partial_report.md"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(text)
    console.print(f"\n[yellow]Partial output saved to:[/yellow] {output_path.resolve()}")


def run():
    check_api_key()

    mode_label = "[green]LIVE AWS[/green]" if DATA_MODE == "live" else "[yellow]MOCK DATA[/yellow]"
    console.print(Panel(
        Text(f"AWS Cloud Cost Optimization Advisor\nPowered by DeepAgents + Claude on Bedrock  |  {mode_label}", justify="center"),
        style="bold blue",
    ))
    console.print()

    if DATA_MODE == "live":
        try:
            from tools.aws_client import get_account_id
            account_id = get_account_id()
        except Exception as e:
            console.print(f"[bold red]Failed to connect to AWS:[/bold red] {_friendly_error(e)}")
            sys.exit(1)
        os.environ["AWS_ACCOUNT_ID"] = account_id  # picked up by config.py when agents are imported
        region = os.environ.get("AWS_DEFAULT_REGION", "")
        console.print(f"[bold]Account:[/bold] {account_id}  |  [bold]Region:[/bold] {region}  |  [bold]Mode:[/bold] [green]Live AWS APIs[/green]")
    else:
        console.print("[bold]Account:[/bold] 123456789012 (mock)  |  [bold]Region:[/bold] us-east-1  |  [bold]Mode:[/bold] [yellow]Mock Data[/yellow]")

    console.print("[bold]Services:[/bold] EC2 · EBS · RDS · S3 · Network · Lambda · CloudWatch · Savings Plans")

    if os.environ.get("LANGCHAIN_TRACING_V2", "").lower() == "true" and os.environ.get("LANGCHAIN_API_KEY"):
        project = os.environ.get("LANGCHAIN_PROJECT", "default")
        console.print(f"[bold]Tracing:[/bold] [magenta]LangSmith[/magenta] → project [cyan]{project}[/cyan]")
    else:
        console.print("[bold]Tracing:[/bold] [dim]disabled (set LANGCHAIN_TRACING_V2=true + LANGCHAIN_API_KEY to enable)[/dim]")

    console.print()
    console.print(Rule("[dim]Live Agent Activity[/dim]"))

    # ── Build orchestrator ─────────────────────────────────────────────────────
    try:
        from agents.orchestrator import build_orchestrator
        orchestrator = build_orchestrator()
    except Exception as e:
        console.print(f"\n[bold red]Failed to initialise agent:[/bold red] {_friendly_error(e)}")
        sys.exit(1)

    # ── Streaming loop ─────────────────────────────────────────────────────────
    final_text_parts = []
    current_subagent = None
    tool_call_buffer: dict = {}
    announced_tools: set = set()

    try:
        for chunk, metadata in orchestrator.stream(
            {"messages": [{"role": "user", "content": USER_PROMPT}]},
            stream_mode="messages",
        ):
            ctype = type(chunk).__name__

            if DEBUG:
                content_preview = repr(chunk.content)[:120] if hasattr(chunk, "content") else ""
                tc_names = [tc.get("name") for tc in (getattr(chunk, "tool_calls", None) or [])]
                console.print(f"[dim]DBG {ctype} content={content_preview} tool_calls={tc_names}[/dim]")

            # --- Streaming AI text ---
            if ctype == "AIMessageChunk":
                text = _extract_text(chunk.content)
                if text:
                    console.print(text, end="", highlight=False)
                    final_text_parts.append(text)

                for tc in getattr(chunk, "tool_call_chunks", None) or []:
                    idx = tc.get("index", 0)
                    if idx not in tool_call_buffer:
                        tool_call_buffer[idx] = {"name": "", "args": ""}

                    if tc.get("name") and not tool_call_buffer[idx]["name"]:
                        tool_call_buffer[idx]["name"] = tc["name"]
                        tool_name = tc["name"]
                        if tool_name == "write_todos":
                            console.print("\n[dim]  📋 Orchestrator planning tasks...[/dim]")
                            announced_tools.add(idx)
                        elif tool_name == "task":
                            pass  # agent name extracted below as args accumulate
                        elif tool_name not in INTERNAL_TOOLS:
                            _print_tool_call(tool_name)
                            announced_tools.add(idx)

                    if tc.get("args"):
                        tool_call_buffer[idx]["args"] += tc["args"]
                        # Detect task agent name from partial JSON as args stream in
                        buf = tool_call_buffer[idx]
                        if buf.get("name") == "task" and not buf.get("agent_announced"):
                            m = re.search(r'"subagent_type"\s*:\s*"([^"]+)"', buf["args"])
                            if not m:
                                m = re.search(r'"(?:name|agent_name|subagent_name)"\s*:\s*"([^"]+)"', buf["args"])
                            if m:
                                agent_name = m.group(1)
                                _print_agent_start(agent_name)
                                current_subagent = agent_name
                                buf["agent_announced"] = True

            # --- Complete AI message ---
            elif ctype == "AIMessage":
                # Capture final text that arrived as a complete message (not streamed)
                text = _extract_text(chunk.content)
                if text and text not in "".join(final_text_parts):
                    console.print(text, end="", highlight=False)
                    final_text_parts.append(text)

                if chunk.tool_calls:
                    for tc in chunk.tool_calls:
                        name = tc.get("name", "")
                        args = tc.get("args", {})
                        if name == "task":
                            agent_name = _extract_subagent_name(args)
                            _print_agent_start(agent_name)
                            current_subagent = agent_name
                        elif name and name not in INTERNAL_TOOLS:
                            idx_match = next(
                                (i for i, v in tool_call_buffer.items() if v["name"] == name and i not in announced_tools),
                                None,
                            )
                            if idx_match is not None:
                                _print_tool_call(name)
                tool_call_buffer = {}
                announced_tools = set()

            # --- Tool result ---
            elif ctype == "ToolMessage":
                tool_name = getattr(chunk, "name", "") or ""
                if tool_name == "task":
                    if current_subagent:
                        _print_agent_done(current_subagent)
                        current_subagent = None
                elif tool_name and tool_name not in INTERNAL_TOOLS:
                    console.print(f"  [dim]    ↩ {tool_name}() returned[/dim]")

    except KeyboardInterrupt:
        console.print("\n\n[yellow]Interrupted by user (Ctrl+C).[/yellow]")
        _save_partial(final_text_parts)
        sys.exit(0)

    except Exception as e:
        console.print(f"\n\n[bold red]Error during analysis:[/bold red] {_friendly_error(e)}")
        _save_partial(final_text_parts)
        sys.exit(1)

    # ── Persist report ─────────────────────────────────────────────────────────
    final_message = "".join(final_text_parts).strip()
    if not final_message:
        console.print("\n[yellow]Warning: no output was collected. The orchestrator may not have produced a final report.[/yellow]")
        sys.exit(1)

    # Markdown (always saved)
    md_path = _RUN_DIR / "report.md"
    md_path.write_text(final_message)

    # HTML — if the report agent didn't already write it, generate it here
    html_path = _RUN_DIR / "report.html"
    if not html_path.exists():
        from tools.report_tools import save_html_report
        save_html_report(final_message)

    console.print()
    console.print(Rule())
    console.print(f"\n[green bold]Run dir: [/green bold] {_RUN_DIR.resolve()}")
    console.print(f"[green bold]Markdown:[/green bold] {md_path.resolve()}")
    if html_path.exists():
        console.print(f"[green bold]HTML:    [/green bold] {html_path.resolve()}")
    json_path = _RUN_DIR / "report.json"
    if json_path.exists():
        console.print(f"[green bold]JSON:    [/green bold] {json_path.resolve()}")


if __name__ == "__main__":
    run()
