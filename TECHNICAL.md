# Technical Documentation — AWS Cloud Cost Optimization Advisor

This document explains the internal implementation: how agents are built, how data flows, how the streaming loop works, and the design decisions behind each layer.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Technology Stack](#2-technology-stack)
3. [Entry Point — main.py](#3-entry-point--mainpy)
4. [Configuration — config.py](#4-configuration--configpy)
5. [DeepAgent Pattern](#5-deepagent-pattern)
6. [Orchestrator Agent](#6-orchestrator-agent)
7. [Specialist Sub-Agents](#7-specialist-sub-agents)
8. [Tool Layer](#8-tool-layer)
9. [AWS Client & CloudWatch Batching](#9-aws-client--cloudwatch-batching)
10. [Mock / Live Mode Dispatch](#10-mock--live-mode-dispatch)
11. [Report Agent & Output Tools](#11-report-agent--output-tools)
12. [Streaming Loop Implementation](#12-streaming-loop-implementation)
13. [Cost Calculator](#13-cost-calculator)
14. [Data Flow — End to End](#14-data-flow--end-to-end)
15. [Environment Variables Reference](#15-environment-variables-reference)
16. [Error Handling Strategy](#16-error-handling-strategy)
17. [Known Constraints & Design Decisions](#17-known-constraints--design-decisions)

---

## 1. System Overview

The system is a **hierarchical multi-agent pipeline**. A single orchestrator LLM plans and delegates work to 8 specialist agents. Each specialist calls Python functions (tools) that query AWS or return mock data. A final report agent consolidates all findings into JSON, HTML, and Markdown.

### Architecture Diagram

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                          ENTRY POINT  (main.py)                             ║
║  CLI: python3 main.py [--mock | --live] [--debug]                           ║
║  • Loads .env  • Sets AWS_DATA_MODE  • Creates output/<timestamp>/          ║
║  • In live mode: calls STS → sets AWS_ACCOUNT_ID                            ║
╚══════════════════════════╦═══════════════════════════════════════════════════╝
                           ║  orchestrator.stream(USER_PROMPT, stream_mode="messages")
                           ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║              ORCHESTRATOR AGENT  (agents/orchestrator.py)                   ║
║              Model: Claude Haiku 4.5 via Amazon Bedrock                     ║
║              Built with: create_deep_agent()  [DeepAgents / LangGraph]      ║
║                                                                              ║
║  Built-in tools:                                                             ║
║    write_todos  ─── Plans task list before acting (internal, ~30–60s)       ║
║    task()       ─── Delegates to a named sub-agent (blocks until done)      ║
╚══════╦═══════╦══════╦═══════╦═══════╦════════╦════════╦═══════╦════════════╝
       ║       ║      ║       ║       ║        ║        ║       ║
Phase 1: Compute & Database          Phase 2: Storage & Commitments    Phase 3
       ║       ║      ║       ║       ║        ║        ║       ║
       ▼       ▼      ▼       ▼       ▼        ▼        ▼       ▼
   ┌───────┐┌──────┐┌─────┐┌──────┐┌───────┐┌──────┐┌──────┐┌────────────┐
   │  EC2  ││ RDS  ││ EBS ││  S3  ││Network││Saving││Lambda││ CloudWatch │
   │ Agent ││Agent ││Agent││Agent ││ Agent ││ Agent││Agent ││   Agent    │
   └───┬───┘└──┬───┘└──┬──┘└──┬───┘└───┬───┘└──┬───┘└──┬───┘└─────┬──────┘
       │       │       │      │        │       │       │           │
       ▼       ▼       ▼      ▼        ▼       ▼       ▼           ▼
   ┌─────────────────────────────── TOOL LAYER (tools/*.py) ──────────────────┐
   │                                                                           │
   │  Each agent calls its own tools. All tools follow the same pattern:      │
   │                                                                           │
   │  def get_xyz() -> str:                                                    │
   │      return _live_xyz() if USE_LIVE else json.dumps(_MOCK["xyz"])        │
   │                    │                              │                       │
   │                    ▼                              ▼                       │
   │            ┌──────────────┐              ┌──────────────┐                │
   │            │  boto3 calls │              │  Mock JSON   │                │
   │            │  (AWS APIs)  │              │  data/       │                │
   │            │              │              │  mock_*.json │                │
   │            │ • describe_* │              └──────────────┘                │
   │            │ • list_*     │                                               │
   │            │ • get_*      │                                               │
   │            │ • CloudWatch │                                               │
   │            │   batch      │                                               │
   │            └──────┬───────┘                                               │
   │                   │                                                       │
   │            ┌──────▼───────────────────────────────────────┐              │
   │            │         aws_client.py  (shared layer)         │              │
   │            │  USE_LIVE  _REGION  get_client()              │              │
   │            │  get_cw_metrics_batch()  (up to 500/call)     │              │
   │            └──────────────────────────────────────────────┘              │
   └───────────────────────────────────────────────────────────────────────────┘
       │       │       │      │        │       │       │           │
       └───────┴───────┴──────┴────────┴───────┴───────┴───────────┘
                                       │
                         JSON findings per service
                                       │
                                       ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                  REPORT AGENT  (agents/report_agent.py)                     ║
║                                                                              ║
║  Receives: combined JSON findings from all 8 specialist agents              ║
║                                                                              ║
║  Step 1: save_json_report(findings_json)                                    ║
║           └─→  output/<timestamp>/report.json                               ║
║                                                                              ║
║  Step 2: LLM writes Markdown report (streamed back to main.py)             ║
║                                                                              ║
║  Step 3: save_html_report(markdown)                                         ║
║           └─→  output/<timestamp>/report.html  (dark-themed, self-contained)║
╚══════════════════════════╦═══════════════════════════════════════════════════╝
                           ║
                           ▼
╔══════════════════════════════════════════════════════════════════════════════╗
║                    OUTPUT  (output/<timestamp>/)                             ║
║                                                                              ║
║   report.md    ← full Markdown, written by main.py from streamed chunks     ║
║   report.html  ← dark-themed HTML, written by save_html_report()           ║
║   report.json  ← structured findings JSON, written by save_json_report()   ║
║                                                                              ║
║   Each run gets its own folder  →  previous runs never overwritten          ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### Execution Phases

| Phase | Agents | Focus |
|---|---|---|
| Phase 1 — Compute & Database | EC2 → RDS → EBS | Largest spend drivers — instance rightsizing, DB waste, storage artifacts |
| Phase 2 — Storage & Commitments | S3 → Network → Savings | Lifecycle policies, NAT/LB waste, RI/SP coverage gaps |
| Phase 3 — Serverless & Observability | Lambda → CloudWatch | Memory rightsizing, deprecated runtimes, log retention |
| Phase 4 — Synthesis | Report Agent | Consolidates all findings, writes JSON + HTML + Markdown |

---

## 2. Technology Stack

| Component | Library / Service |
|---|---|
| Agent framework | `deepagents` (LangChain/LangGraph) |
| LLM | Claude Haiku 4.5 via Amazon Bedrock (`ChatBedrockConverse`) |
| AWS SDK | `boto3` |
| Streaming / orchestration | LangGraph `stream_mode="messages"` |
| Terminal UI | `rich` |
| HTML generation | `markdown` (Python) |
| Env management | `python-dotenv` |
| Tracing (optional) | LangSmith (zero-code via env vars) |

---

## 3. Entry Point — main.py

`main.py` is the single entry point. It handles CLI parsing, mode setup, the streaming loop, and final report persistence.

### Startup sequence

```python
# 1. Load .env before anything else
load_dotenv()

# 2. Parse --live / --mock / --debug
_args = _parser.parse_args()

# 3. Set AWS_DATA_MODE before tools are imported
#    (tools read USE_LIVE at import time from aws_client.py)
os.environ["AWS_DATA_MODE"] = "live" if _args.live else "mock"

# 4. Create timestamped output directory and expose it to report_tools
_RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_DIR = Path("output") / _RUN_TIMESTAMP
_RUN_DIR.mkdir(parents=True, exist_ok=True)
os.environ["REPORT_OUTPUT_DIR"] = str(_RUN_DIR)
```

**Why `AWS_DATA_MODE` must be set before imports:**  
`tools/aws_client.py` evaluates `USE_LIVE` at module level:
```python
USE_LIVE: bool = os.environ.get("AWS_DATA_MODE", "mock").lower() == "live"
```
If agents were imported before this env var was set, `USE_LIVE` would be permanently `False`.

### Live mode — account ID bootstrap

In live mode, `main.py` calls STS to resolve the real account ID, then injects it into the environment before the orchestrator (and `config.py`) are imported:

```python
from tools.aws_client import get_account_id
account_id = get_account_id()                  # STS GetCallerIdentity
os.environ["AWS_ACCOUNT_ID"] = account_id      # config.py reads this at import time
```

`config.py` does:
```python
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "123456789012")
```
The `"123456789012"` default only appears in mock mode — it is never sent to AWS.

---

## 4. Configuration — config.py

`config.py` constructs the Bedrock model instances used by all agents.

```python
_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

ORCHESTRATOR_MODEL = ChatBedrockConverse(
    model_id=_MODEL_ID,
    region_name=AWS_DEFAULT_REGION,
    timeout=300,
)
ANALYST_MODEL = ChatBedrockConverse(
    model_id=_MODEL_ID,
    region_name=AWS_DEFAULT_REGION,
    timeout=300,
)
```

**Key decisions:**

- **Model instances, not strings.** DeepAgents accepts `BaseChatModel` directly. Passing an instance (vs. a string like `"bedrock_converse:..."`) allows setting `timeout=300`, which overrides boto3's default 60-second read timeout. Without this, complex analyses time out mid-stream.

- **`us.` cross-region inference prefix.** Claude Haiku 4.5 requires this prefix to support Bedrock prompt caching. DeepAgents automatically enables prompt caching for Bedrock models; using the old `anthropic.claude-3-haiku-20240307-v1:0` model ID causes `AccessDeniedException` because that model does not support the `ConverseStream` API with caching.

- **Single model ID for both orchestrator and analysts.** Both use the same model. Two separate instances exist to allow future differentiation (e.g., a more powerful model for the orchestrator) without changing all call sites.

---

## 5. DeepAgent Pattern

DeepAgents wraps LangGraph to make multi-agent delegation straightforward.

### Creating an agent

```python
from deepagents import create_deep_agent

orchestrator = create_deep_agent(
    model=ORCHESTRATOR_MODEL,
    system_prompt="...",
    subagents=[ec2_agent, ebs_agent, ...],  # list of sub-agent dicts
)
```

### Sub-agent definition

Each sub-agent is a plain Python `dict`:

```python
ec2_agent = {
    "name": "ec2-analyst",          # used to identify this agent in task() calls
    "description": "...",           # seen by the orchestrator when it decides who to call
    "system_prompt": "...",         # LLM instructions for this specialist
    "tools": [get_ec2_inventory,    # plain Python functions — no @tool decorator needed
               get_elastic_ips,
               analyze_ec2_rightsizing],
    "model": ANALYST_MODEL,         # BaseChatModel instance
}
```

### Built-in orchestrator tools

DeepAgents injects two tools automatically into the orchestrator:

| Tool | Purpose |
|---|---|
| `task(subagent_type, task)` | Delegate work to a named sub-agent; blocks until the sub-agent returns |
| `write_todos` | Internal planning tool — orchestrator writes its task list before acting |

Sub-agents get only the tools in their `"tools"` list — they cannot call other sub-agents directly.

---

## 6. Orchestrator Agent

**File:** `agents/orchestrator.py`

The orchestrator's system prompt defines a 4-phase execution plan:

```
Phase 1 — Compute & Database  →  ec2-analyst, rds-analyst, ebs-analyst
Phase 2 — Storage & Network   →  s3-analyst, network-analyst, savings-analyst
Phase 3 — Serverless          →  lambda-analyst, cloudwatch-analyst
Phase 4 — Synthesis           →  report-agent (receives all prior findings)
```

The prompt explicitly tells the orchestrator to:
1. Run all 8 specialists — never skip one even if expected savings are small
2. Compile findings into a single JSON keyed by service name
3. Pass that JSON to `report-agent`
4. Print the Markdown returned by `report-agent` verbatim as its final output

The `build_orchestrator()` factory is called lazily inside `run()`, after all env vars are set:

```python
from agents.orchestrator import build_orchestrator
orchestrator = build_orchestrator()
```

This delay ensures `config.py` picks up `AWS_ACCOUNT_ID` from the environment (set by the STS call in live mode) when `orchestrator.py` is first imported.

---

## 7. Specialist Sub-Agents

**Directory:** `agents/`

Each specialist follows an identical structure:

```python
# agents/ec2_agent.py
from tools.ec2_tools import get_ec2_inventory, get_elastic_ips, analyze_ec2_rightsizing
from config import ANALYST_MODEL

EC2_SYSTEM_PROMPT = """..."""

ec2_agent = {
    "name": "ec2-analyst",
    "description": "...",
    "system_prompt": EC2_SYSTEM_PROMPT,
    "tools": [get_ec2_inventory, get_elastic_ips, analyze_ec2_rightsizing],
    "model": ANALYST_MODEL,
}
```

The system prompt for each specialist instructs the LLM to:
1. Call its data-fetching tools in a specific order
2. Reason over the returned JSON
3. Return **only** a structured JSON findings object (no prose)

| Agent | Tools |
|---|---|
| `ec2-analyst` | `get_ec2_inventory`, `get_elastic_ips`, `analyze_ec2_rightsizing` |
| `ebs-analyst` | `get_ebs_volumes`, `get_ebs_snapshots`, `get_unused_amis`, `analyze_ebs_optimization`, `analyze_orphaned_snapshots_and_amis` |
| `rds-analyst` | `get_rds_inventory`, `get_rds_reserved_instances`, `analyze_rds_rightsizing` |
| `s3-analyst` | `get_s3_inventory`, `analyze_s3_optimization` |
| `network-analyst` | `get_load_balancers`, `get_nat_gateways`, `get_data_transfer_costs`, `analyze_network_optimization` |
| `savings-analyst` | `get_current_spend_summary`, `get_active_savings_plans`, `get_active_reserved_instances`, `get_coverage_analysis`, `analyze_savings_plan_recommendations` |
| `lambda-analyst` | `get_lambda_inventory`, `analyze_lambda_optimization` |
| `cloudwatch-analyst` | `get_cloudwatch_log_groups`, `get_cloudwatch_custom_metrics`, `get_cloudwatch_alarms`, `analyze_cloudwatch_optimization` |
| `report-agent` | `save_json_report`, `save_html_report` |

---

## 8. Tool Layer

**Directory:** `tools/`

Every tool is a plain Python function with a docstring. The docstring is used by DeepAgents as the tool description that the LLM sees when deciding which tool to call.

### Tool anatomy

Each tool file follows this structure:

```python
# Module-level imports and constants
import json
from pathlib import Path
from tools.aws_client import USE_LIVE, get_client, get_cw_metrics_batch

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_xyz.json").read_text())

# ── public tools ───────────────────────────────
def get_xyz_inventory() -> str:
    """Docstring — this is the tool description the LLM sees."""
    return _live_xyz_inventory() if USE_LIVE else json.dumps(_MOCK["xyz"], indent=2)

# ── shared analysis ────────────────────────────
def _run_xyz_analysis(data: list) -> str:
    # Pure Python — works on both live and mock data
    ...

# ── live implementations ───────────────────────
def _live_xyz_inventory() -> str:
    client = get_client("xyz")
    ...
```

**All tools return `str` (JSON).** The LLM receives text, not Python objects — returning a JSON string is the natural interface.

### Analysis tools

Analysis tools (`analyze_*`) re-use the same `_run_*_analysis()` function regardless of mode:

```python
def analyze_ec2_rightsizing() -> str:
    instances = json.loads(_live_ec2_inventory() if USE_LIVE else json.dumps(_MOCK["instances"]))
    return _run_ec2_analysis(instances)
```

This means the business logic is tested once and works identically in both modes.

---

## 9. AWS Client & CloudWatch Batching

**File:** `tools/aws_client.py`

### Client factory

```python
_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"max_attempts": 3, "mode": "adaptive"},
)

def get_client(service: str):
    return boto3.client(service, region_name=_REGION, config=_BOTO_CONFIG)
```

All tools call `get_client("ec2")`, `get_client("rds")`, etc. No tool creates its own `boto3.client()` directly — this keeps retry config and region in one place.

`_REGION` is the single source of truth for the AWS region:

```python
_REGION: str = os.environ.get("AWS_DEFAULT_REGION", "")
```

Empty string means boto3 falls back to its own region resolution chain (env var → config file → instance metadata). **No region is ever hardcoded anywhere** — even fallbacks use `_REGION`.

### CloudWatch batch helper

CloudWatch `GetMetricData` supports up to **500 metrics per call**. `get_cw_metrics_batch()` batches arbitrary numbers of metric queries efficiently:

```python
def get_cw_metrics_batch(metric_queries: list, days: int = 30) -> dict:
    for batch_start in range(0, len(metric_queries), 500):
        batch = metric_queries[batch_start : batch_start + 500]
        # ... single API call for up to 500 metrics
        for r in resp["MetricDataResults"]:
            values = r.get("Values", [])
            if stat == "Maximum":   results[r["Id"]] = max(values)
            elif stat == "Sum":     results[r["Id"]] = sum(values)
            else:                   results[r["Id"]] = sum(values) / len(values)
    return results  # {query_id: float}
```

Usage in tools (example from `ec2_tools.py`):

```python
queries = []
id_map = {}
for idx, inst in enumerate(instances):
    qid = f"ec2{idx}cpu"
    queries.append({"id": qid, "namespace": "AWS/EC2",
                    "metric_name": "CPUUtilization",
                    "dimensions": [{"Name": "InstanceId", "Value": inst["instance_id"]}],
                    "stat": "Average"})
    id_map[qid] = inst["instance_id"]

raw = get_cw_metrics_batch(queries)  # one API call for all instances
for qid, val in raw.items():
    cw_by_instance[id_map[qid]]["avg_cpu"] = val
```

This pattern avoids N separate `GetMetricStatistics` calls (one per resource), which would be slow and hit API rate limits on large accounts.

---

## 10. Mock / Live Mode Dispatch

Mock mode lets the system run without AWS credentials, using pre-built JSON fixtures in `data/`.

### Dispatch pattern (every tool)

```python
def get_ec2_inventory() -> str:
    return _live_ec2_inventory() if USE_LIVE else json.dumps(_MOCK["instances"], indent=2)
```

`USE_LIVE` is a module-level boolean in `aws_client.py`, evaluated once at import time:

```python
USE_LIVE: bool = os.environ.get("AWS_DATA_MODE", "mock").lower() == "live"
```

### Why module-level (not function-level)

If `USE_LIVE` were checked inside each function via `os.environ.get(...)`, the mode could theoretically change mid-run. Fixing it at import time makes the mode immutable for the lifetime of the process and avoids repeated env lookups.

### Mock data files

```
data/
  mock_ec2.json
  mock_ebs.json
  mock_rds.json
  mock_s3.json
  mock_network.json
  mock_lambda.json
  mock_cloudwatch.json
  mock_savings.json
```

Each file mirrors the exact structure that the live implementation returns, so the analysis functions (`_run_*_analysis`) operate on identical shapes in both modes.

---

## 11. Report Agent & Output Tools

**Files:** `agents/report_agent.py`, `tools/report_tools.py`

The report agent is the last agent called. It receives the complete findings JSON from the orchestrator and is responsible for all three output files.

### save_json_report

```python
def save_json_report(findings_json: str) -> str:
    data = json.loads(findings_json)           # validate JSON
    out = _run_dir() / "report.json"
    out.write_text(json.dumps(data, indent=2))
    return f"JSON report saved to {out.resolve()}"
```

### save_html_report

Converts Markdown → HTML using the `markdown` package with extensions:

```python
body = md.markdown(
    markdown_content,
    extensions=["tables", "fenced_code", "toc"],
)
```

The HTML is a self-contained dark-themed page (no external CDN dependencies). All styles are inlined.

### Timestamped output directory

`_run_dir()` reads `REPORT_OUTPUT_DIR` from the environment:

```python
def _run_dir() -> Path:
    return Path(os.environ.get("REPORT_OUTPUT_DIR", "output"))
```

`main.py` sets this at startup:

```python
_RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_DIR = Path("output") / _RUN_TIMESTAMP
os.environ["REPORT_OUTPUT_DIR"] = str(_RUN_DIR)
```

Result: every run writes to its own folder (`output/20260722_143512/`). Previous runs are never overwritten.

### Fallback HTML generation

If the report agent's `save_html_report` call fails or is skipped by the LLM, `main.py` generates the HTML itself as a fallback:

```python
html_path = _RUN_DIR / "report.html"
if not html_path.exists():
    from tools.report_tools import save_html_report
    save_html_report(final_message)
```

---

## 12. Streaming Loop Implementation

**File:** `main.py` — `run()` function

The orchestrator is executed with LangGraph's message streaming:

```python
for chunk, metadata in orchestrator.stream(
    {"messages": [{"role": "user", "content": USER_PROMPT}]},
    stream_mode="messages",
):
```

### Chunk types yielded

| Type | When | What we do |
|---|---|---|
| `AIMessageChunk` | Streaming LLM output | Extract text, detect tool calls |
| `AIMessage` | Complete message (rare) | Capture any unseen text |
| `ToolMessage` | Tool call returned | Print "agent done" banner |

**`AIMessage` is almost never yielded** in `stream_mode="messages"` — all LLM output arrives as `AIMessageChunk`. The `AIMessage` branch exists as a defensive fallback.

### Bedrock content extraction

Bedrock returns `AIMessageChunk.content` as a **list of blocks**, not a plain string:

```python
# What Bedrock actually sends:
chunk.content = [{"type": "text", "text": "...", "index": 0}]

# Naive check fails:
if isinstance(chunk.content, str):  # always False for Bedrock
    ...
```

The `_extract_text()` helper handles both formats:

```python
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
```

### Agent name detection via regex

The orchestrator calls sub-agents via the `task()` tool. In streaming mode, tool call arguments arrive in fragments across multiple `AIMessageChunk` events. To print the `▶ [EC2]` banner as early as possible, the code accumulates the partial JSON args and runs a regex as soon as enough has arrived:

```python
if tc.get("args"):
    tool_call_buffer[idx]["args"] += tc["args"]
    buf = tool_call_buffer[idx]
    if buf.get("name") == "task" and not buf.get("agent_announced"):
        m = re.search(r'"subagent_type"\s*:\s*"([^"]+)"', buf["args"])
        if not m:
            m = re.search(r'"(?:name|agent_name|subagent_name)"\s*:\s*"([^"]+)"', buf["args"])
        if m:
            agent_name = m.group(1)
            _print_agent_start(agent_name)
            buf["agent_announced"] = True
```

`write_todos` (the orchestrator's internal planning tool) is detected on the first chunk name and immediately prints a planning message — it can take 30–60 seconds before the first real agent starts:

```python
if tool_name == "write_todos":
    console.print("\n[dim]  📋 Orchestrator planning tasks...[/dim]")
```

### Internal tool suppression

DeepAgents injects several file-system tools (`write_file`, `read_file`, `edit_file`, `ls`, `glob`, `grep`) that are implementation details. These are hidden from the terminal output:

```python
INTERNAL_TOOLS = {"write_todos", "write_file", "read_file", "edit_file", "ls", "glob", "grep"}
```

---

## 13. Cost Calculator

**File:** `tools/cost_calculator.py`

Shared pricing constants and estimation helpers used by multiple tool files. All prices are approximate us-east-1 on-demand rates.

| Function | Input | Output |
|---|---|---|
| `estimate_ec2_rightsize_savings(instance_type, monthly_cost)` | Current instance type + cost | Recommended type, savings USD and % |
| `estimate_ebs_gp2_to_gp3_savings(size_gb)` | Volume size | Savings from 20% price reduction |
| `estimate_s3_lifecycle_savings(size_gb, cold_percent)` | Bucket size + cold data % | Savings from STANDARD → Glacier IR |
| `estimate_lambda_rightsize_savings(current_mb, avg_used_mb, monthly_cost)` | Memory config + actual usage | Recommended memory, savings USD and % |

Lambda rightsizing snaps to valid memory sizes (128, 256, 512, 1024, 1536, 2048, 3008 MB) and adds a 1.3× headroom buffer over observed usage:

```python
optimal_mb = min(
    next((m for m in [128, 256, 512, 1024, 1536, 2048, 3008] if m >= avg_used_mb * 1.3), current_mb),
    current_mb,
)
```

---

## 14. Data Flow — End to End

The sequence below shows the exact order of operations for a single run, including which process/module is active at each step and what data is produced.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  STARTUP  (main.py — module level, before run())                            │
│                                                                             │
│  1. load_dotenv()           → os.environ ← .env file values                │
│  2. parse CLI args          → DATA_MODE = "live" | "mock"                  │
│  3. os.environ["AWS_DATA_MODE"] = DATA_MODE                                │
│  4. create _RUN_DIR = output/20260724_143512/   (mkdir)                    │
│  5. os.environ["REPORT_OUTPUT_DIR"] = str(_RUN_DIR)                        │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
                  ┌──────────────▼──────────────┐
                  │  LIVE MODE ONLY             │
                  │  STS GetCallerIdentity()    │
                  │  → os.environ["AWS_ACCOUNT_ID"]  │
                  └──────────────┬──────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────────────┐
│  AGENT INITIALISATION  (main.py → run())                                    │
│                                                                             │
│  from agents.orchestrator import build_orchestrator                         │
│    → imports config.py          (reads AWS_ACCOUNT_ID, AWS_DEFAULT_REGION) │
│    → imports all 8 agent files  (registers tools + system prompts)         │
│    → imports all tool files     (USE_LIVE set, _MOCK loaded from data/)    │
│  orchestrator = build_orchestrator()   → LangGraph compiled state machine  │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────────────┐
│  STREAMING LOOP  orchestrator.stream(USER_PROMPT, stream_mode="messages")   │
│                                                                             │
│  ┌─ Orchestrator LLM turn ──────────────────────────────────────────────┐  │
│  │  tool_call: write_todos(...)   ← internal planning, ~30–60s         │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌─ Phase 1: Compute & Database ────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  tool_call: task("ec2-analyst", ...)                                 │  │
│  │    └── EC2 Agent LLM                                                 │  │
│  │          get_ec2_inventory()                                         │  │
│  │            ├── LIVE: ec2.describe_instances() + CW batch            │  │
│  │            └── MOCK: data/mock_ec2.json → _MOCK["instances"]        │  │
│  │          analyze_ec2_rightsizing(instance_id)   [called per instance]│  │
│  │            └── _run_ec2_analysis() → savings estimates              │  │
│  │          get_elastic_ips()                                           │  │
│  │            ├── LIVE: ec2.describe_addresses()                       │  │
│  │            └── MOCK: _MOCK["elastic_ips"]                           │  │
│  │          → returns JSON findings string to orchestrator             │  │
│  │                                                                       │  │
│  │  tool_call: task("rds-analyst", ...)    ← same pattern              │  │
│  │  tool_call: task("ebs-analyst", ...)    ← same pattern              │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌─ Phase 2: Storage & Commitments ─────────────────────────────────────┐  │
│  │  tool_call: task("s3-analyst", ...)                                  │  │
│  │  tool_call: task("network-analyst", ...)                             │  │
│  │  tool_call: task("savings-analyst", ...)   ← Cost Explorer API      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌─ Phase 3: Serverless & Observability ────────────────────────────────┐  │
│  │  tool_call: task("lambda-analyst", ...)                              │  │
│  │  tool_call: task("cloudwatch-analyst", ...)                          │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌─ Phase 4: Synthesis ──────────────────────────────────────────────────┐  │
│  │  tool_call: task("report-agent", combined_findings_json)             │  │
│  │    └── Report Agent LLM                                              │  │
│  │          save_json_report(findings_json)                             │  │
│  │            └──→ output/<timestamp>/report.json                       │  │
│  │          [LLM writes Markdown text — streamed as AIMessageChunks]   │  │
│  │          save_html_report(markdown)                                  │  │
│  │            └──→ output/<timestamp>/report.html                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  Each AIMessageChunk → _extract_text() → final_text_parts list            │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼────────────────────────────────────────────┐
│  OUTPUT PERSISTENCE  (main.py — after loop ends)                            │
│                                                                             │
│  final_message = "".join(final_text_parts)                                 │
│  (_RUN_DIR / "report.md").write_text(final_message)                        │
│                                                                             │
│  if not (_RUN_DIR / "report.html").exists():    ← fallback only            │
│      save_html_report(final_message)                                        │
│                                                                             │
│  Prints paths:                                                              │
│    Run dir:  output/20260724_143512/                                        │
│    Markdown: output/20260724_143512/report.md                              │
│    HTML:     output/20260724_143512/report.html                            │
│    JSON:     output/20260724_143512/report.json                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 15. Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | Yes (live) | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Yes (live) | AWS secret key |
| `AWS_SESSION_TOKEN` | No | For temporary/assumed-role credentials |
| `AWS_DEFAULT_REGION` | Yes (live) | Target region, e.g. `us-east-1` |
| `AWS_DATA_MODE` | Internal | Set by `main.py` — `"live"` or `"mock"` |
| `AWS_ACCOUNT_ID` | Internal | Set by `main.py` from STS in live mode |
| `REPORT_OUTPUT_DIR` | Internal | Set by `main.py` — timestamped run dir path |
| `LANGCHAIN_TRACING_V2` | No | `"true"` to enable LangSmith tracing |
| `LANGCHAIN_API_KEY` | No | LangSmith API key |
| `LANGCHAIN_PROJECT` | No | LangSmith project name |

Variables marked **Internal** are set programmatically by `main.py` and should not be set manually.

---

## 16. Error Handling Strategy

### Per-tool (AWS API calls)

Every live tool wraps AWS API calls in `try/except`:

```python
try:
    ver = s3.get_bucket_versioning(Bucket=name)
    versioning_enabled = ver.get("Status") == "Enabled"
except Exception:
    versioning_enabled = False  # safe default, analysis continues
```

Failures on individual resources never abort the whole tool. The analysis continues with partial data.

### CloudWatch batch fallback

If the entire batch call fails, all metric IDs default to `0.0`:

```python
except Exception:
    for q in batch:
        results[q["id"]] = 0.0
```

Tools that use `None` instead of `0` for "data unavailable" fields add an explicit guard before using the value:

```python
# lambda_tools.py — avg_memory_used_mb is None (Lambda Insights not available)
if avg_used is not None and avg_used > 0 and avg_used < current_mb * 0.3:
    ...

# cloudwatch_tools.py — last_viewed_days_ago is None (CW API doesn't expose it)
last_viewed = dashboard.get("last_viewed_days_ago")
if last_viewed is not None and last_viewed > 90:
    ...
```

Using `None` (not `0`) prevents silent false negatives where a zero default would cause a guard like `if x > 90` to always fail.

### Streaming loop

```python
except KeyboardInterrupt:
    _save_partial(final_text_parts)   # saves whatever was collected
    sys.exit(0)

except Exception as e:
    console.print(f"Error: {_friendly_error(e)}")
    _save_partial(final_text_parts)
    sys.exit(1)
```

`_friendly_error()` maps common exception class names and message substrings to human-readable messages (throttling, expired tokens, invalid credentials, network timeouts, etc.) so engineers don't have to decode boto3 tracebacks.

---

## 17. Known Constraints & Design Decisions

### Prompt caching requires the `us.` prefix

DeepAgents automatically enables Bedrock prompt caching. This requires the cross-region inference model ID prefix (`us.anthropic.claude-haiku-4-5-20251001-v1:0`). The older `anthropic.claude-3-haiku-20240307-v1:0` model does not support the `ConverseStream` API with caching enabled and throws `AccessDeniedException`.

### Lambda memory metrics require Lambda Insights

Standard CloudWatch (`AWS/Lambda`) does not expose actual memory usage — only invocations, duration, errors, and throttles. Real memory usage requires the Lambda Insights extension, which publishes to the `LambdaInsights` namespace. The field `avg_memory_used_mb` is set to `None` in live mode to prevent the analysis from silently skipping rightsizing checks (a `0` value would make `0 < current_mb * 0.3` always false).

### CloudWatch dashboards have no last-viewed timestamp

The `ListDashboards` API returns name, ARN, and last-modified time — but not last-viewed time. `last_viewed_days_ago` is set to `None` in live mode; unused dashboard detection only fires on mock data where the fixture includes this field.

### S3 object access patterns not available from standard APIs

S3 Access Analyzer and S3 Storage Lens provide last-access data but require separate setup. In live mode, `objects_not_accessed_in_90_days_percent` is set to `0`, which means lifecycle policy recommendations only trigger based on `lifecycle_policy_exists` and `versioning_enabled` flags (not cold data percentage) in live mode.

### Cost Explorer requires explicit enablement

`get_current_spend_summary()`, `get_coverage_analysis()`, and `_live_data_transfer()` all call the Cost Explorer API (`ce`). Cost Explorer must be enabled in the AWS Console and the IAM principal must have `ce:Get*` permissions. All three functions wrap the API call in `try/except` and return partial/empty data if it fails, rather than crashing the whole run.

### RDS pricing model detection

To determine whether an RDS instance is covered by a Reserved Instance (and thus already at a discounted price), `_live_rds_inventory()` fetches all active RIs upfront before iterating instances:

```python
active_ri_classes: set = set()
for page in rds.get_paginator("describe_reserved_db_instances").paginate():
    for ri in page["ReservedDBInstances"]:
        if ri.get("State") == "active":
            active_ri_classes.add(ri["DBInstanceClass"])

# Then per instance:
pricing_model = "reserved" if db_class in active_ri_classes else "on-demand"
```

This is a class-level match (e.g., `db.m5.large`), not an instance-level match. If an account has two `db.m5.large` instances but only one RI, both will be marked `"reserved"`. The RI coverage gap analysis in `savings_tools.py` catches this at the account level via Cost Explorer.
