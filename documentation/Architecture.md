# Architecture — AWS Cloud Cost Advisor

How a natural-language question becomes an answer grounded in live AWS data, and
what the DeepAgents framework contributes to that flow.

## At a glance

```mermaid
flowchart LR
    U(["👤 User asks<br/>a question"]) --> S["⚙️ FastAPI<br/>server.py"]
    S --> A["🧠 DeepAgent<br/>Claude Haiku 4.5"]

    A -->|"I need data"| T["🔧 Tools<br/>call_aws_api<br/>execute_python"]
    T --> AWS["☁️ AWS<br/>account"]
    AWS -->|"raw JSON"| T
    T -->|"result"| A

    A -->|"I have the answer"| ANS(["💬 Answer<br/>streamed to browser"])

    style U fill:#3a66d8,stroke:#2848a8,color:#fff
    style A fill:#0e9267,stroke:#0a6b4c,color:#fff
    style AWS fill:#c44e1f,stroke:#8f3814,color:#fff
    style ANS fill:#0e9267,stroke:#0a6b4c,color:#fff
```

The agent loops between **think** and **call a tool** as many times as it needs, then
answers. Everything below is that loop in more detail.

---

- [1. Design thesis](#1-design-thesis)
- [2. System architecture](#2-system-architecture)
- [3. What `create_deep_agent` builds](#3-what-create_deep_agent-builds)
- [4. The reasoning loop](#4-the-reasoning-loop)
- [5. Worked example](#5-worked-example)
- [6. Two request paths, one agent](#6-two-request-paths-one-agent)
- [7. Persistence](#7-persistence)
- [8. Design trade-offs](#8-design-trade-offs)

---

## 1. Design thesis

**Two universal tools instead of dozens of specific ones.**

A conventional AWS cost tool hardcodes one function per question: `get_ec2_costs()`,
`find_unattached_volumes()`, `check_rds_rightsizing()`. Every new question needs new code.

This system ships exactly two domain tools:

| Tool | What it does |
|---|---|
| `call_aws_api(service, operation, params)` | Any boto3 operation on any AWS service |
| `execute_python(code, context_json)` | Arbitrary Python over the JSON that came back |

Together they span the entire AWS API surface plus arbitrary computation over the
results. The agent composes them at runtime to answer questions nobody wrote code for.
"Which gp2 volumes should be gp3?" and "project my Q3 spend if I stop the dev fleet"
both work without a code change — the reasoning that used to live in Python now lives
in the model's tool-selection loop.

**DeepAgents supplies the loop.** The project does not implement agent orchestration.
`create_deep_agent()` provides the reason → call tool → observe → repeat cycle, plus
planning and scratchpad capabilities, and LangGraph persists every step.

---

## 2. System architecture

```mermaid
flowchart TB
    subgraph BROWSER["🌐 Browser — static/index.html"]
        direction LR
        CHATUI["Chat Panel<br/><i>freeform questions</i>"]
        PANELUI["Analysis Panel<br/><i>Refresh button</i>"]
    end

    subgraph SERVER["⚙️ FastAPI — server.py"]
        direction LR
        RCHAT["POST /chat"]
        RANA["POST /analysis/run"]
        RSESS["GET/DELETE /sessions"]
    end

    subgraph AGENT["🧠 DeepAgent — agents/chat_agent.py"]
        LLM["Claude Haiku 4.5<br/><i>via Bedrock ConverseStream</i>"]
        LOOP{"tool_calls<br/>present?"}
    end

    subgraph TOOLS["🔧 Tools"]
        direction LR
        AWSAPI["call_aws_api"]
        PYEXEC["execute_python"]
        BUILTIN["9 DeepAgents built-ins<br/><i>write_todos, task, filesystem</i>"]
    end

    AWS["☁️ AWS APIs<br/>ce · ec2 · rds · elbv2 · s3 · cloudwatch"]
    SUBPROC["🐍 Python subprocess<br/><i>10s timeout</i>"]

    LGDB[("langgraph.db<br/><i>agent checkpoints</i>")]
    CHDB[("chat_history.db<br/><i>UI display history</i>")]

    CHATUI -->|"message + thread_id"| RCHAT
    PANELUI -->|"scripted prompt"| RANA
    RCHAT --> LLM
    RANA --> LLM

    LLM --> LOOP
    LOOP -->|"yes"| TOOLS
    TOOLS -->|"ToolMessage"| LLM
    LOOP -->|"no — final answer"| SSE

    AWSAPI <-->|"boto3"| AWS
    PYEXEC <-->|"stdout"| SUBPROC

    LLM <-.->|"read + write<br/>every step"| LGDB
    RCHAT -.->|"write after response"| CHDB
    RSESS -.->|"read"| CHDB

    SSE["📡 SSE stream<br/><i>text/event-stream</i>"]
    SSE -->|"text · tool_start · tool_end"| CHATUI
    SSE -->|"log · result"| PANELUI

    style AGENT fill:#0e9267,stroke:#0a6b4c,color:#fff
    style AWS fill:#c44e1f,stroke:#8f3814,color:#fff
    style LGDB fill:#6b52b8,stroke:#4e3a8a,color:#fff
    style CHDB fill:#6b52b8,stroke:#4e3a8a,color:#fff
    style SSE fill:#a86e00,stroke:#7a5000,color:#fff
```

**Layers**

| Layer | File | Responsibility |
|---|---|---|
| UI | `static/index.html` | Chat panel + analysis panel, SSE consumption, Markdown rendering |
| Server | `server.py` | Routing, SSE framing, analysis-JSON extraction, session CRUD |
| Agent | `agents/chat_agent.py` | Model config, system prompt, tool registration |
| Tools | `tools/aws_api.py`, `tools/python_repl.py` | AWS access, computation |
| Persistence | `chat_db.py` + LangGraph checkpointer | Two SQLite databases |

---

## 3. What `create_deep_agent` builds

The call in [`agents/chat_agent.py:89`](../agents/chat_agent.py#L89) is deliberately minimal:

```python
create_deep_agent(
    model=model,                 # ChatBedrockConverse — Claude Haiku 4.5
    tools=_TOOLS,                # [call_aws_api, execute_python]
    checkpointer=checkpointer,   # AsyncSqliteSaver → langgraph.db
    system_prompt=_SYSTEM_PROMPT + context_block,
)
```

Four arguments. DeepAgents 0.6.12 expands that into a compiled LangGraph state machine
with **11 registered tools** — the 2 supplied plus 9 injected by default middleware:

```mermaid
flowchart LR
    subgraph CUSTOM["Supplied by this project"]
        T1["call_aws_api"]
        T2["execute_python"]
    end

    subgraph INJECTED["Injected by DeepAgents middleware"]
        direction TB
        M1["<b>TodoListMiddleware</b><br/>write_todos"]
        M2["<b>SubAgentMiddleware</b><br/>task"]
        M3["<b>FilesystemMiddleware</b><br/>ls · read_file · write_file<br/>edit_file · glob · grep · execute"]
    end

    CUSTOM --> AGENT["Compiled<br/>DeepAgent<br/><b>11 tools</b>"]
    INJECTED --> AGENT

    style CUSTOM fill:#0e9267,stroke:#0a6b4c,color:#fff
    style INJECTED fill:#3a66d8,stroke:#2848a8,color:#fff
    style AGENT fill:#445870,stroke:#2c3a4a,color:#fff
```

| Middleware | Tools added | Purpose in this system |
|---|---|---|
| `TodoListMiddleware` | `write_todos` | Agent plans multi-step work before executing — used heavily on the 7-step analysis run |
| `FilesystemMiddleware` | `ls`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `execute` | Virtual scratchpad for staging intermediate results between steps |
| `SubAgentMiddleware` | `task` | Spawn an isolated sub-agent with its own context window |
| `PatchToolCallsMiddleware` | — | Repairs malformed tool calls instead of failing the turn |
| `AnthropicPromptCachingMiddleware` | — | Caches the large static system prompt across turns, cutting token cost |

**Architectural consequence.** Because 9 tools arrive uninvited, the frontend progress
log cannot simply count `tool_end` events — a `write_todos` call would corrupt the
step counter. The UI therefore filters to `call_aws_api` and `execute_python` only.
The same holds for the analysis log in [`server.py:358`](../server.py#L358).

> **Note.** `task` and `execute` are registered but not exercised by current prompts.
> `execute` in particular is a shell-execution capability that is present and reachable —
> worth knowing about when reviewing the trust boundary.

---

## 4. The reasoning loop

This is the heart of the system — where "DeepAgent" earns its place.

```mermaid
sequenceDiagram
    participant U as Browser
    participant S as FastAPI
    participant A as DeepAgent
    participant M as Claude Haiku 4.5
    participant T as Tools
    participant DB as langgraph.db

    U->>S: POST /chat {message, thread_id}
    S->>A: agent.astream(msg, thread_id)
    A->>DB: load checkpoint for thread_id
    DB-->>A: prior messages

    rect rgb(232, 245, 240)
    note over A,T: Reasoning loop — repeats until no tool_calls
    loop until final answer
        A->>M: system prompt + history + tool schemas
        M-->>A: AIMessage + tool_calls
        A->>DB: checkpoint
        A-->>S: stream chunk
        S-->>U: SSE tool_start
        A->>T: dispatch tool
        T-->>A: ToolMessage (JSON or stdout)
        A->>DB: checkpoint
        S-->>U: SSE tool_end
    end
    end

    M-->>A: AIMessage, no tool_calls
    A-->>S: text chunks
    S-->>U: SSE text (streamed tokens)
    S->>S: save to chat_history.db
    S-->>U: SSE done
```

**Loop mechanics**

1. **Context assembly** — the checkpointer rehydrates the full prior conversation for
   this `thread_id`, so turn 5 still knows what turn 1 fetched.
2. **Decision** — the model sees the system prompt (tool docs) plus the account ID,
   region, and today's date prepended fresh to the message on every request — never
   baked into the system prompt itself, since that's built once at process startup
   and would otherwise go stale — and decides which tool to call, or that it has
   enough to answer.
3. **Dispatch** — LangGraph routes to the tool node and appends the result as a
   `ToolMessage`.
4. **Termination** — the loop exits when the model returns an `AIMessage` with no
   `tool_calls`. There is no fixed step limit; the model decides when it is done.
5. **Error recovery** — both tools return errors *as strings* rather than raising.
   A failed boto3 call yields `{"error": "..."}` and a Python exception yields the
   stderr trace, so the model can read the failure and retry with corrected arguments
   on the next iteration. This is why the loop is resilient without try/catch in the
   server.

---

## 5. Worked example

**Question:** *"Which of my EBS volumes are wasting money?"*

```mermaid
flowchart TB
    Q["<b>User:</b> Which EBS volumes are wasting money?"] --> I1

    I1["<b>Iteration 1</b><br/>Model reasons: I need the volume inventory"]
    I1 --> C1["call_aws_api('ec2', 'describe_volumes')"]
    C1 --> R1["ToolMessage: 47 volumes, raw JSON"]

    R1 --> I2["<b>Iteration 2</b><br/>Model reasons: filter unattached, compute cost"]
    I2 --> C2["execute_python(code, context_json=volumes)<br/><i>filter State=='available', sum Size × $0.10</i>"]
    C2 --> R2["ToolMessage: 6 unattached, 840 GB, $84.00/mo"]

    R2 --> I3["<b>Iteration 3</b><br/>Model reasons: gp2→gp3 opportunity too"]
    I3 --> C3["execute_python(code, context_json=volumes)<br/><i>filter VolumeType=='gp2'</i>"]
    C3 --> R3["ToolMessage: 22 gp2 volumes, ~$61.60/mo savings"]

    R3 --> F["<b>Iteration 4</b><br/>No tool_calls — model composes<br/>Markdown answer with real IDs and totals"]
    F --> OUT["<b>Streamed to browser</b><br/>token by token via SSE"]

    style Q fill:#3a66d8,stroke:#2848a8,color:#fff
    style C1 fill:#c44e1f,stroke:#8f3814,color:#fff
    style C2 fill:#3a66d8,stroke:#2848a8,color:#fff
    style C3 fill:#3a66d8,stroke:#2848a8,color:#fff
    style OUT fill:#0e9267,stroke:#0a6b4c,color:#fff
```

Nothing in the codebase knows what an "unattached volume" is. The model derived the
concept, wrote the filter, applied a price, and assembled the answer — three tool calls,
zero domain-specific code.

---

## 6. Two request paths, one agent

The agent is built **once** at server startup ([`server.py:53`](../server.py#L53)) and stored
as `app.state.agent`. Both endpoints call `.astream()` on that same object. Three things
differentiate them:

```mermaid
flowchart TB
    AGENT["<b>app.state.agent</b><br/>single instance, built at startup"]

    subgraph PATH1["Path A — /chat"]
        direction TB
        P1A["<b>thread_id:</b> persistent<br/>thread-a1b2c3"]
        P1B["<b>prompt:</b> user's raw message"]
        P1C["<b>consumed:</b> AIMessageChunk text"]
        P1D["<b>events:</b> text, tool_start, tool_end"]
        P1E["<b>stored:</b> chat_history.db"]
    end

    subgraph PATH2["Path B — /analysis/run"]
        direction TB
        P2A["<b>thread_id:</b> fresh each run<br/>analysis-4f2a"]
        P2B["<b>prompt:</b> _build_analysis_prompt()<br/>7 scripted steps + JSON schema"]
        P2C["<b>consumed:</b> execute_python stdout"]
        P2D["<b>events:</b> log, tool_end, result"]
        P2E["<b>stored:</b> _ANALYSIS dict, in memory"]
    end

    AGENT --> PATH1
    AGENT --> PATH2

    style AGENT fill:#0e9267,stroke:#0a6b4c,color:#fff
    style PATH1 fill:#e8eefb,stroke:#3a66d8
    style PATH2 fill:#fdf3e0,stroke:#a86e00
```

**Why a fresh thread for analysis.** Because LangGraph loads context keyed by
`thread_id`, the analysis run starts with **zero prior context** — it never sees your
chat, and your chat never sees it. The analysis needs a deterministic, repeatable run,
not whatever you happened to ask ten minutes earlier.

**Why the prose is discarded.** The analysis endpoint reads the `execute_python`
ToolMessage content — not the model's narration — and parses JSON out of it
([`server.py:370`](../server.py#L370)). The prompt ends with *"execute_python must be your
FINAL action. Print ONLY the JSON."* Three fallback parse strategies handle the cases
where the model wraps it in a code fence or adds commentary anyway.

---

## 7. Persistence

Two SQLite databases in the project root, serving two different consumers.

```mermaid
flowchart LR
    A["DeepAgent"] <-->|"AsyncSqliteSaver<br/>WAL mode"| L[("<b>langgraph.db</b><br/>checkpoints<br/>checkpoint_blobs<br/>checkpoint_writes")]
    S["FastAPI routes"] <-->|"chat_db.py<br/>sqlite3 sync"| C[("<b>chat_history.db</b><br/>sessions<br/>messages")]

    style L fill:#6b52b8,stroke:#4e3a8a,color:#fff
    style C fill:#6b52b8,stroke:#4e3a8a,color:#fff
```

| | `langgraph.db` | `chat_history.db` |
|---|---|---|
| Owner | LangGraph library | This project (`chat_db.py`) |
| Format | Serialized binary blobs | Plain text SQL rows |
| Purpose | Rebuild agent context | Render the history sidebar |
| Written | Every message and tool step | After each completed response |
| Read by | `AsyncSqliteSaver` before each `astream()` | `GET /sessions`, `GET /sessions/{id}/messages` |

They cannot be merged: the sidebar needs human-readable message text, and extracting
that from LangGraph's internal blob format would mean deserializing library internals
that change between releases.

Both are auto-created on first run and listed in `.gitignore`.

> **Known gap.** Each analysis run creates an `analysis-*` thread that is never deleted —
> the delete endpoints only clean up threads removed from the chat sidebar. `langgraph.db`
> accumulates orphaned analysis checkpoints over time.

---

## 8. Design trade-offs

**What this architecture buys**

- New questions need no new code — the tool surface already spans all of AWS
- One system prompt is the entire "business logic"; behaviour changes are prompt edits
- Tool errors become model input rather than crashes, so the loop self-corrects
- Adding a service means nothing: `call_aws_api("kinesis", ...)` already works

**What it costs**

- **Non-deterministic** — the same question may take a different tool path each run,
  which is why evaluation needs seeded fixtures and multi-run scoring rather than
  single-shot assertions
- **Unbounded blast radius** — `call_aws_api` has no operation allowlist
  ([`tools/aws_api.py:47`](../tools/aws_api.py#L47) does `getattr(client, operation)`), so
  IAM policy is the only thing preventing a destructive call
- **`execute_python` is not sandboxed** despite its docstring — it is a plain
  `subprocess.run` sharing the user, environment, filesystem, and network, with AWS
  credentials in scope. The 10-second timeout is the only limit.
- **Latency and token cost scale with iteration count**, and the model chooses that count

**The alternative that was replaced.** An earlier design used a master orchestrator
delegating to eight specialist sub-agents (EC2, EBS, RDS, S3, Network, Savings, Lambda,
CloudWatch) with roughly ten tool modules. That structure is still described in
`README.md`, which is now stale. It was more predictable but required a code change for
every new question — the two-universal-tools design trades that predictability for
open-ended coverage.

---

## Related documents

- [`TECHNICAL.md`](TECHNICAL.md) — implementation reference: routes, SSE event tables, schemas
- [`Infra.md`](Infra.md) — deployment architecture: AWS resources, IAM, network, redeploy process
- [`../static/deepagent-flow.html`](../static/deepagent-flow.html) — the same flow as a rendered visual page
