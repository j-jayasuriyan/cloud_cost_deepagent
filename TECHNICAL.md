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

---

## 1. System Overview

A conversational chat interface for querying any AWS account data — costs, resources, usage, security, and trends. The user types a question; the agent calls live AWS APIs and streams the answer back.

### Architecture

```
Browser (static/index.html)
    │  POST /chat  (SSE stream)
    │  GET  /sessions, /sessions/{id}/messages
    │  DELETE /sessions/{id}, /sessions
    ▼
FastAPI  (server.py)
    │  lifespan: STS → account context, AsyncSqliteSaver, build_chat_agent()
    │  /chat → SSE event_stream → agent.astream()
    ▼
DeepAgent  (agents/chat_agent.py)
    │  create_deep_agent(model, tools, checkpointer, system_prompt)
    │  Checkpoints: langgraph.db  (SQLite, WAL mode)
    ├──▶  call_aws_api(service, operation, params)   → any boto3 call → JSON
    └──▶  execute_python(code, context_json)          → sandboxed subprocess
```

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
├── server.py            # FastAPI app, SSE streaming, session endpoints
├── chat_db.py           # chat_history.db — session list + message log
├── agents/
│   └── chat_agent.py    # build_chat_agent() using create_deep_agent
├── tools/
│   ├── aws_api.py       # call_aws_api() — universal boto3 caller
│   └── python_repl.py   # execute_python() — sandboxed Python subprocess
├── static/
│   └── index.html       # full chat UI (vanilla JS, no build step)
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

Injected at build time with runtime-known facts so the agent never calls tools to look them up:

```
## Current AWS Session
- Account ID: 123456789012
- Region: us-east-1
- Caller ARN: arn:aws:iam::123456789012:user/...
- Today's date: <date.today().isoformat() at server startup>

Always use <today> as the end date when constructing date ranges.
```

`today` is set at startup so the agent uses the correct end date for Cost Explorer queries (its training knowledge stops in 2025).

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

1. Calls STS `GetCallerIdentity` → sets `AWS_ACCOUNT_ID` env var
2. Opens `langgraph.db` briefly to set `PRAGMA journal_mode=WAL` (allows concurrent connections for delete endpoints)
3. Creates `AsyncSqliteSaver` as the agent's checkpointer
4. Builds the agent with `build_chat_agent(saver, ctx)` → stored in `app.state.agent`

### Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Serve `static/index.html` |
| `GET` | `/health` | Liveness check |
| `GET` | `/status` | STS credential check — returns `{ok, account, arn, region}` |
| `GET` | `/sessions` | List all sessions from `chat_history.db` |
| `GET` | `/sessions/{id}/messages` | Messages for one session |
| `DELETE` | `/sessions/{id}` | Delete one session from both DBs |
| `DELETE` | `/sessions` | Delete all sessions from both DBs |
| `POST` | `/chat` | SSE stream — runs the agent |

### `/chat` — SSE stream

```python
async for chunk, _metadata in app.state.agent.astream(
    {"messages": [{"role": "user", "content": req.message}]},
    config={"configurable": {"thread_id": req.thread_id}},
    stream_mode="messages",
):
```

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

Manages `chat_history.db` with two tables:

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

### Credential check on load

Calls `GET /status` before showing the chat UI. On failure, shows an error banner with remediation steps and disables the input. On success, shows account ID + region in the header.

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

| `type` | `data` |
|---|---|
| `text` | Partial assistant text token |
| `tool_start` | Tool name (e.g. `"call_aws_api"`) |
| `tool_end` | Tool name |
| `done` | `""` |
| `session_corrupted` | New `thread_id` to switch to |
| `error` | Error message (truncated to 500 chars) |

---

## 11. Session Persistence

### Two databases

| DB | Format | Managed by | Purpose |
|---|---|---|---|
| `langgraph.db` | SQLite (WAL) | `AsyncSqliteSaver` | Agent message history + tool call checkpoints per `thread_id` |
| `chat_history.db` | SQLite | `chat_db.py` | Session list + message log for the history panel |

`langgraph.db` is the agent's memory — it contains the full conversation including tool call/result pairs. `chat_history.db` stores only user/assistant text for display in the history panel.

### Thread IDs

New chat: `'thread-' + Math.random().toString(36).slice(2)` (browser-generated).
Corrupted session recovery: `'thread-' + secrets.token_hex(6)` (server-generated).

---

## 12. Environment Variables

| Variable | Required | Set by | Description |
|---|---|---|---|
| `AWS_ACCESS_KEY_ID` | Yes | `.env` / shell | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Yes | `.env` / shell | AWS secret key |
| `AWS_SESSION_TOKEN` | For SSO/assumed-role | `.env` / shell | Temporary session token |
| `AWS_DEFAULT_REGION` | Yes | `.env` / shell | Target region, e.g. `us-east-1` |
| `AWS_ACCOUNT_ID` | Internal | `server.py` lifespan | Set from STS at startup; injected into agent system prompt |

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

`GET /status` is called on page load. If STS returns `ExpiredTokenException` or similar, the UI shows an error banner with instructions to refresh credentials and restart the server.
