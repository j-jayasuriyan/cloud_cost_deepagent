# Technical Documentation — AWS Cloud Cost Advisor

Internal implementation reference: architecture, data flow, streaming, and design decisions.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Technology Stack](#2-technology-stack)
3. [Project Structure](#3-project-structure)
4. [Entry Points](#4-entry-points)
5. [Agent — chat_agent.py](#5-agent--chat_agentpy)
6. [Tools](#6-tools)
7. [Server — server.py](#7-server--serverpy)
8. [Chat History — chat_db.py](#8-chat-history--chat_dbpy)
9. [UI — static/index.html](#9-ui--staticindexhtml)
10. [Streaming Protocol (SSE)](#10-streaming-protocol-sse)
11. [Session Persistence](#11-session-persistence)
12. [Environment Variables](#12-environment-variables)
13. [Error Handling](#13-error-handling)
14. [Cost Optimization Panel](#14-cost-optimization-panel)

---

## 1. System Overview

A split-panel web app for AWS cost intelligence. The left panel is a conversational chat interface where the user can ask any question about their AWS account. The right panel is a cost optimization dashboard that runs a structured analysis on demand and displays actionable recommendations with visualizations.

### Architecture

```
Browser (static/login.html, static/index.html)
    │  POST /login, POST /logout
    │  GET/POST/DELETE /credentials   (AWS keys for the account being analysed)
    │  POST /chat            (SSE — chat responses)
    │  POST /analysis/run    (SSE — optimization analysis)
    │  GET  /analysis        (current analysis state)
    │  GET  /sessions, /sessions/{id}/messages
    │  DELETE /sessions/{id}, /sessions
    ▼
FastAPI  (server.py)
    │  require_login middleware: every route but /login and /health needs a session cookie
    │  lifespan: check_bedrock_invoke(), WAL mode, AsyncSqliteSaver, build_chat_agent()
    │  /chat          → SSE event_stream → agent.astream()
    │  /analysis/run  → SSE stream()    → agent.astream() on fresh thread
    ▼
DeepAgent  (agents/chat_agent.py)
    │  create_deep_agent(model, tools, checkpointer, system_prompt)
    │  Checkpoints: langgraph.db  (SQLite, WAL mode)
    ├──▶  call_aws_api(service, operation, params)   → any boto3 call → JSON
    └──▶  execute_python(code, context_json)          → sandboxed subprocess
```

Two AWS identities are in play and must not be confused: the **deployment
account** (whatever credentials the server process itself resolves — env vars,
instance profile, or task role — used only to call Bedrock) and the
**analysed account** (the keys a signed-in user pastes into the Credentials
panel, held in memory per login session, used only to call `call_aws_api`).
`deployment.py` health-checks the former; `credentials.py`/`aws_session.py`
manage the latter.

---

## 2. Technology Stack

| Component | Library / Service |
|---|---|
| Agent framework | `deepagents` — `create_deep_agent` |
| LLM | Claude Haiku 4.5 via Amazon Bedrock (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) |
| LLM client | `langchain-aws` — `ChatBedrockConverse` |
| Web server | `FastAPI` + `uvicorn` |
| Streaming | Server-Sent Events (`text/event-stream`) |
| Agent memory | `langgraph-checkpoint-sqlite` — `AsyncSqliteSaver` (WAL mode) |
| Chat history | `sqlite3` (built-in) — `chat_history.db` |
| AWS SDK | `boto3` |
| Env management | `python-dotenv` |

---

## 3. Project Structure

```
cloud_cost_deepAgent/
├── chat.py              # launcher — opens browser, starts uvicorn
├── server.py            # FastAPI app, SSE streaming, auth middleware, session endpoints
├── auth.py              # login sessions — single shared username/password
├── credentials.py       # per-login-session AWS keys for the analysed account (in memory)
├── aws_session.py       # boto3 client cache, keyed by (login session, service, region)
├── deployment.py        # health checks for the deployment's own Bedrock/STS credentials
├── chat_db.py           # chat_history.db — session list + message log
├── agents/
│   └── chat_agent.py    # build_chat_agent() using create_deep_agent
├── tools/
│   ├── aws_api.py       # call_aws_api() — universal boto3 caller
│   └── python_repl.py   # execute_python() — sandboxed Python subprocess
├── static/
│   ├── login.html       # sign-in page
│   └── index.html       # full chat + analysis UI (vanilla JS, no build step)
├── langgraph.db         # LangGraph checkpoints (agent memory per thread)
└── chat_history.db      # session metadata + message log (for history panel)
```

---

## 4. Entry Points

### `chat.py` — launcher

Checks for missing AWS credentials, changes directory to the project root, opens the browser after a 1.5 s delay, then starts uvicorn:

```python
uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
```

If run outside the venv, it re-execs itself with the venv Python via `os.execv`.

### Direct uvicorn

```bash
.venv/bin/python3 -m uvicorn server:app --host 127.0.0.1 --port 8000
```

---

## 5. Agent — `chat_agent.py`

### `build_chat_agent(checkpointer, aws_ctx)`

Called once at server startup (inside the FastAPI lifespan). Returns a compiled LangGraph state machine.

```python
create_deep_agent(
    model=ChatBedrockConverse(model_id=_MODEL_ID, region_name=region, timeout=300),
    tools=[call_aws_api, execute_python],
    checkpointer=checkpointer,      # AsyncSqliteSaver → langgraph.db
    system_prompt=_SYSTEM_PROMPT + context_block,
)
```

### System prompt

`_SYSTEM_PROMPT` (module-level constant) documents the two tools with examples and rules. It uses `'''` (single-triple-quote) because the prompt body contains code examples with `"""`.

### Context block

Injected once at build time (server startup), and deliberately contains
**no facts that could go stale or leak across sessions** — no account ID, no
region, no date:

```
## Current AWS Session
The account ID, region, and today's date under analysis are stated at the top
of each request. Use them directly — do NOT call APIs, and do NOT rely on
training data, to determine any of them.
```

**Why not the account ID:** a single agent instance is shared by every login
session, and different sessions can have different AWS keys entered against
different accounts. Baking an account ID in at startup would leak one user's
account into another's answers.

**Why not today's date either — this used to be baked in here, and it was a
real bug.** An earlier version set `today = date.today().isoformat()` once,
at `build_chat_agent()` time, and embedded it directly in this block. Since
`build_chat_agent()` runs exactly once per process (in `server.py`'s
`lifespan`), that date only reflected reality on the day the server was
started — a long-lived process (weeks of uptime, common for a small
single-instance deployment) would silently keep answering as if it were
still that day, producing wrong Cost Explorer date ranges with no error or
warning. Fixed by moving the date to the same per-request mechanism as the
account ID.

`server.py`'s `_account_preamble()` resolves the *current* session's account
via STS and computes `date.today()` fresh, prepending both to each
individual request:

```
[AWS context — account 111122223333, region us-east-1, today's date 2026-08-17. Use these directly; do not look them up.]

<the user's actual message>
```

This runs on every `/chat` and `/analysis/run` call, so both the account and
the date always reflect reality at request time — not whatever they were
when the process happened to start (see `credentials.py`, `aws_session.py`
for the account-resolution side).

### Model

`us.anthropic.claude-haiku-4-5-20251001-v1:0` — the `us.` cross-region inference prefix is required for Bedrock prompt caching, which `create_deep_agent` enables automatically. The older `anthropic.claude-3-haiku-20240307-v1:0` ID throws `AccessDeniedException` with the `ConverseStream` API.

---

## 6. Tools

The agent has exactly two custom tools (plus DeepAgents built-ins: `write_todos`, filesystem ops, `task`).

### `call_aws_api(service, operation, params)` — `tools/aws_api.py`

Calls any boto3 client method and returns the JSON response.

```python
client = boto3.client(service, region_name=region, config=_BOTO_CONFIG)
response = getattr(client, operation)(**json.loads(params))
response.pop("ResponseMetadata", None)
return json.dumps(response, indent=2, cls=_Encoder)
```

- `_BOTO_CONFIG`: 10 s connect, 60 s read, adaptive retry up to 3 attempts
- `_Encoder`: handles `datetime` → ISO string, `Decimal` → float (common in boto3 responses)
- Errors are caught and returned as `{"error": "...", "service": "...", "operation": "..."}` so the agent can self-correct

### `execute_python(code, context_json)` — `tools/python_repl.py`

Runs Python in an isolated subprocess with a 10 s timeout.

```python
subprocess.run([sys.executable, "-c", full_code], capture_output=True, timeout=10)
```

`context_json` is injected as `_ctx` (pre-parsed) so the agent doesn't need to call `json.loads` inside its code. On timeout or non-zero exit, stderr is returned so the agent can fix its code.

Use cases: totals, averages, trend analysis, "what if" simulations, filtering, sorting.

---

## 7. Server — `server.py`

### Lifespan

Runs once at startup:

1. `deployment.check_bedrock_invoke()` — confirms the deployment's own credentials can reach STS *and* invoke the Bedrock model; logs the verdict (does not raise — sign-in is refused later if this failed, per-request, rather than crashing startup)
2. Opens `langgraph.db` briefly to set `PRAGMA journal_mode=WAL` (allows concurrent connections for delete endpoints)
3. Creates `AsyncSqliteSaver` as the agent's checkpointer
4. Builds the agent with `build_chat_agent(saver, ctx)` → stored in `app.state.agent`

### Authentication & credentials

A `require_login` middleware guards every route except `/login` and `/health`.
It checks the `cca_session` cookie against `auth.py`'s in-memory session
store; HTML requests without a valid cookie are redirected to `/login`,
everything else gets a `401`.

Two distinct credential sets flow through the app — see the diagram in
[§1](#1-system-overview) — and neither ever touches disk:

- **Login** (`auth.py`) — one shared username/password (env-configurable),
  gates access to the app itself.
- **Analysed-account keys** (`credentials.py`, `aws_session.py`) — the AWS
  access key / secret / session token a signed-in user pastes into the
  Credentials panel. Validated against STS *before* being accepted, stored
  in memory keyed by login session only, and used to build a per-session
  boto3 client cache. Clearing credentials or logging out drops both the
  keys and any cached clients for that session.

### Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/login` | Serve `static/login.html` |
| `POST` | `/login` | Verify username/password, set the session cookie |
| `POST` | `/logout` | Clear login session, analysed-account credentials, and cached boto3 clients |
| `GET` | `/credentials` | Current analysed-account identity for this session (no key material) |
| `POST` | `/credentials` | Validate and store analysed-account keys for this session |
| `DELETE` | `/credentials` | Forget the analysed-account keys for this session |
| `GET` | `/` | Serve `static/index.html` |
| `GET` | `/health` | Liveness check (public, no auth) |
| `GET` | `/deployment` | Deployment's own Bedrock/STS health — `{ok, error, account_id, region}` |
| `GET` | `/status` | STS check on the *analysed* account for this session — `{ok, account, arn, region}` |
| `GET` | `/sessions` | List all chat sessions from `chat_history.db` |
| `GET` | `/sessions/{id}/messages` | Messages for one chat session |
| `DELETE` | `/sessions/{id}` | Delete one chat session from both DBs |
| `DELETE` | `/sessions` | Delete all chat sessions from both DBs |
| `POST` | `/chat` | SSE stream — runs the agent for chat |
| `GET` | `/analysis` | Returns current analysis state (`status`, `result`, `updated_at`, `logs`) |
| `POST` | `/analysis/run` | SSE stream — runs the optimization analysis agent |

### `/chat` — SSE stream

```python
async for chunk, _metadata in app.state.agent.astream(
    {"messages": [{"role": "user",
                   "content": _account_preamble(session) + req.message}]},
    config={"configurable": {"thread_id": req.thread_id, "session_id": session}},
    stream_mode="messages",
):
```

`session_id` in `config.configurable` is how `call_aws_api` (via LangChain's
injected `RunnableConfig`) knows which login session's AWS keys to use —
it's never part of what the model sees. `_account_preamble()` prepends the
resolved account ID and region as plain text in the message content, since
that part *is* meant for the model to read (see [§5](#5-agent--chat_agentpy)).

Chunk types and actions:

| Type | Action |
|---|---|
| `AIMessageChunk` | Extract text → yield `text` SSE event |
| `AIMessage` | Yield `tool_start` for each tool call |
| `ToolMessage` | Yield `tool_end` |

After the loop: saves assistant reply to `chat_db`, bumps session `last_active`, yields `done`.

On exception: if the error contains `"ToolMessage"` and `"tool_calls"`, the checkpoint has a dangling tool call from an interrupted stream → yield `session_corrupted` with a new thread ID. The UI re-sends the message on the fresh thread.

### Session deletion

Two SQLite databases must be cleared on delete:

- `chat_history.db` — synchronous `sqlite3`, always succeeds
- `langgraph.db` — async via `aiosqlite`, best-effort (WAL mode allows concurrent access alongside `AsyncSqliteSaver`)

Both `_delete_lg_thread` and `_delete_lg_all` are fully wrapped in `try/except` so a lock contention never fails the endpoint.

---

## 8. Chat History — `chat_db.py`

Manages `chat_history.db`, stored in the **project root** (same directory as `server.py`). Full path at runtime: `<project_root>/chat_history.db`.

Two tables:

```sql
sessions (thread_id PK, title, created_at, last_active)
messages (id PK, thread_id, role, content, created_at)
```

| Function | Purpose |
|---|---|
| `upsert_session(thread_id, title)` | Create or update session on first message |
| `touch_session(thread_id)` | Bump `last_active` after assistant reply |
| `save_message(thread_id, role, content)` | Persist user/assistant message |
| `get_sessions(limit=100)` | List sessions newest-first |
| `get_messages(thread_id)` | All messages for a session in order |
| `delete_session(thread_id)` | Delete messages + session row |
| `delete_all_sessions()` | Truncate both tables |

Tables are initialised on import via `init_db()`.

---

## 9. UI — `static/index.html`

Single-file vanilla JS app — no build step, no external dependencies.

### Layout

Two-panel split (`#main-split`, CSS `display: flex`):

- **Left panel** (`#chat-panel`, `flex: 1`) — conversational chat with history sidebar
- **Right panel** (`#right-panel`, `width: 50%`) — cost optimization dashboard (see §14)

### Credential check on load

Calls `GET /status` before showing the chat UI. On failure, shows an error banner with remediation steps and disables the input. On success, shows account ID + region in the header, then immediately calls `loadAnalysis()` to restore any previously cached analysis result.

### Chat flow

1. User submits message → `POST /chat` with `{message, thread_id}`
2. SSE reader processes events:
   - `text` → appends to bubble, re-renders inline Markdown
   - `tool_start` → adds a spinning chip below the bubble
   - `tool_end` → marks chip done (✓)
   - `done` → removes cursor, final render
   - `session_corrupted` → shows warning, switches `threadId`, re-sends after 1.2 s
   - `error` → shows red error text
3. `threadId` is a `let` (not `const`) so `session_corrupted` can reassign it

### History panel

Slide-in sidebar triggered by the History button. Lists sessions from `GET /sessions`.

- **Load session**: `GET /sessions/{id}/messages` → renders full conversation, switches active `threadId`
- **Delete session**: optimistic UI (removes DOM element immediately), then `DELETE /sessions/{id}`
- **Clear all**: `DELETE /sessions` → resets to new chat

### Markdown renderer

Inline renderer (no external library). Handles: fenced code blocks, inline code, tables, headings (h1–h3), bold/italic, blockquotes, unordered and ordered lists, paragraphs. Code blocks are protected before other passes to avoid interference.

---

## 10. Streaming Protocol (SSE)

All events are `data: <json>\n\n` with this shape:

```json
{"type": "<event>", "data": "<string>"}
```

### `/chat` events

| `type` | `data` |
|---|---|
| `text` | Partial assistant text token |
| `tool_start` | Tool name (e.g. `"call_aws_api"`) |
| `tool_end` | Tool name |
| `done` | `""` |
| `session_corrupted` | New `thread_id` to switch to |
| `error` | Error message (truncated to 500 chars) |

### `/analysis/run` events

| `type` | `data` |
|---|---|
| `status` | `"running"` |
| `log` | Tool call label, e.g. `"ce.get_cost_and_usage"`, `"execute_python"` |
| `tool_end` | Tool name (`"call_aws_api"` or `"execute_python"`) |
| `result` | JSON string of the full analysis result |
| `error` | Error message (truncated to 300 chars) |
| `done` | `""` |

---

## 11. Session Persistence

### Two databases

Both files live in the **project root** alongside `server.py`:

| File | Format | Managed by | Purpose |
|---|---|---|---|
| `<project_root>/langgraph.db` | SQLite (WAL) | `AsyncSqliteSaver` | Agent message history + tool call checkpoints per `thread_id` |
| `<project_root>/chat_history.db` | SQLite | `chat_db.py` | Session list + message log for the history panel |

`langgraph.db` is the agent's memory — it contains the full conversation including tool call/result pairs, used by LangGraph to resume threads. `chat_history.db` stores only user/assistant text for display in the history panel sidebar.

Both files are created automatically on first run if they don't exist. They are intentionally excluded from version control (listed in `.gitignore`) since they contain account-specific conversation data.

### Thread IDs

New chat: `'thread-' + Math.random().toString(36).slice(2)` (browser-generated).
Corrupted session recovery: `'thread-' + secrets.token_hex(6)` (server-generated).

---

## 12. Environment Variables

These all describe the **deployment account** — the credentials the server
process itself resolves, used only to reach STS (health checks) and Bedrock
(the model). The **analysed account** has no environment variables at all;
its keys come from the UI and live only in `credentials.py`'s in-memory
store (see [§Authentication & credentials](#authentication--credentials)).

| Variable | Required | Set by | Description |
|---|---|---|---|
| `AWS_ACCESS_KEY_ID` | Yes, unless using an instance/task role | `.env` / shell | Deployment account access key |
| `AWS_SECRET_ACCESS_KEY` | Yes, unless using an instance/task role | `.env` / shell | Deployment account secret key |
| `AWS_SESSION_TOKEN` | For SSO/assumed-role | `.env` / shell | Temporary session token |
| `AWS_DEFAULT_REGION` | Yes | `.env` / shell | Region for STS/Bedrock calls, e.g. `us-east-1` |
| `AUTH_USERNAME` | No — defaults to `Admin` | `.env` / shell | Login username (`auth.py`) |
| `AUTH_PASSWORD` | No — defaults to `Admin@123` | `.env` / shell | Login password; override in every real deployment |
| `COOKIE_SECURE` | No — defaults to `true` | `.env` / shell | Set `false` only for plain-HTTP local runs; must stay `true` behind TLS |
| `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `LANGCHAIN_PROJECT` | No | `.env` / shell | Optional LangSmith tracing |

---

## 13. Error Handling

### AWS API errors (`call_aws_api`)

All errors are caught and returned as JSON to the agent:

```json
{"error": "An error occurred (AccessDeniedException)...", "service": "ce", "operation": "get_cost_and_usage"}
```

The agent can read the error and either try an alternative API call or report the specific issue to the user.

### Python sandbox errors (`execute_python`)

- Non-zero exit → returns `"Error (exit N):\n<stderr>"` so the agent can fix its code
- Timeout → returns `"Error: code execution timed out after 10 seconds."`

### Session checkpoint corruption

If a stream is interrupted mid-tool-call, the next request to the same `thread_id` raises:

```
Found AIMessages with tool_calls that do not have a corresponding ToolMessage
```

The server detects this, generates a new thread ID, and yields `session_corrupted`. The browser switches to the new thread and re-sends the user's message automatically.

### Credential expiry

`GET /status` is called on page load and checks the **analysed account's**
credentials via STS. If it returns `ExpiredTokenException` or similar, the UI
shows an error banner prompting the user to re-enter keys in the AWS Account
panel — no server restart needed, since those credentials live in memory per
session (`credentials.py`), not in `.env`.

`GET /deployment` covers the other credential set — the **deployment
account's** own Bedrock/STS access. If that fails, `/login` refuses new
sign-ins (see `deployment.check()` in [§7](#7-server--serverpy)), since no
amount of re-entering analysed-account keys can fix a broken deployment role.

---

## 14. Cost Optimization Panel

The right panel (`#right-panel`) runs a structured analysis of the current AWS account on demand and renders the results as a visual dashboard.

### State machine

`_ANALYSIS` is a module-level dict in `server.py`:

```python
{
    "status":     "idle | running | done | error",
    "result":     None | dict,    # parsed JSON from the agent
    "updated_at": None | str,     # ISO-8601 UTC timestamp
    "logs":       list[str],      # last 30 tool call labels
}
```

State persists in memory for the lifetime of the server process. `GET /analysis` always returns the current state, so a page refresh can restore the last result without re-running.

### Analysis prompt — `_build_analysis_prompt()`

Built fresh on each `/analysis/run` call (so dates are always current). Steps the agent is instructed to follow:

1. `ce.get_cost_and_usage` — current month MTD (Start=`YYYY-MM-01`, End=today)
2. `ce.get_cost_and_usage` — previous month (Start=`prev-YYYY-MM-01`, End=`YYYY-MM-01`)
3. `ec2.describe_instances`
4. `ec2.describe_volumes`
5. `rds.describe_db_instances`
6. `elbv2.describe_load_balancers`
7. `ce.get_savings_plans_purchase_recommendation`
8. `execute_python` — compile everything and `print()` a single JSON object

The agent uses a fresh `thread_id` (`"analysis-<8hex>"`) for each run so it never inherits chat history.

### Analysis result schema

```json
{
  "period":                    "YYYY-MM",
  "is_partial":                true,
  "total_monthly_spend_usd":   0.0,
  "projected_end_usd":         0.0,
  "previous_month_period":     "YYYY-MM",
  "previous_month_usd":        0.0,
  "potential_monthly_savings_usd": 0.0,
  "spend_by_service": [
    { "service": "Amazon EC2", "spend_usd": 0.0 }
  ],
  "recommendations": [
    {
      "category":   "EC2 | EBS | RDS | S3 | Network | Savings",
      "severity":   "high | medium | low",
      "title":      "...",
      "description":"...",
      "estimated_monthly_savings_usd": 0.0,
      "resource_count": 0,
      "action":     "..."
    }
  ]
}
```

`is_partial` is `true` when the current month is still in progress (today is not the last day). When `true`, `total_monthly_spend_usd` is the MTD spend and `projected_end_usd` is a linear projection (`mtd / days_elapsed * days_in_month`).

### Result extraction — `_parse_analysis_json()`

Tries three strategies (in order) to find valid JSON in the agent's output:

1. Direct `json.loads` of the raw text (works when `execute_python` prints clean JSON)
2. ` ```json ... ``` ` code fence extraction
3. Regex for a trailing JSON object containing `"period"`

`execute_python` stdout is tried first (reversed, so most recent call wins); then the full accumulated AIMessage text as a fallback.

### Frontend rendering

The right panel has four view states managed by `setRpState(state)`:

| State | Elements shown |
|---|---|
| `idle` | `#rp-idle` — placeholder with call-to-action |
| `running` | `#rp-running` — animated progress log |
| `done` | `#rp-result` — full dashboard |
| `error` | `#rp-error` — error message |

**Progress log**: each `log` SSE event appends a row with a spinner; each `tool_end` for `call_aws_api` or `execute_python` advances a done-counter, turning the oldest pending spinner to ✓. Built-in DeepAgent tools (`write_todos`, `task`, etc.) emit `tool_end` but are filtered out so they don't miscount.

**KPI tiles** — built dynamically by `renderAnalysis()`:

- *Partial month*: 2×2 grid — "July 2026 (MTD)" / "Projected End of Month" / "June 2026" / "Potential Savings · July 2026"
- *Complete month*: 3-column row — "July 2026" / "June 2026" / "Potential Savings · July 2026"

Period strings (e.g. `"2026-07"`) are formatted client-side by `_formatPeriod()` → `"July 2026"`. No formatting dependency on the agent output.

**Horizontal bars**: top 8 services by spend, sorted descending, width proportional to the largest service's spend. Single sequential blue (`#3987e5`) — magnitude comparison within one dimension.

**Recommendation cards**: sorted by estimated savings descending. Left border and badge colored by severity using the fixed status palette: `#d03b3b` (high ⚠), `#fab219` (medium ●), `#0ca30c` (low ◇). Badge always carries both icon and text label (never color alone).
