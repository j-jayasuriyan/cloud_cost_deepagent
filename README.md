# AWS Cloud Cost Optimization Advisor

## Prerequisites

- Python 3.10+
- An AWS account for the **deployment** (runs the app, needs Bedrock access to
  `us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- The AWS account(s) you want to **analyse** — connected per-session through
  the UI, can be different from the deployment account

---

## Setup

```bash
# 1. Clone and enter the project
git clone <repo-url>
cd cloud_cost_deepAgent

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp .env.example .env
```

Edit `.env` — see [.env.example](.env.example) for the full list. At minimum
you need credentials for the **deployment account** (the one that calls
Bedrock):

```env
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_DEFAULT_REGION=us-east-1
```

Credentials for the account you want to *analyse* are **not** set here — you
enter them through the UI after logging in (see below).

---

## Running

```bash
python3 chat.py
```

This opens `http://127.0.0.1:8000` in your browser. Sign in (defaults to
`Admin` / `Admin@123` — override with `AUTH_USERNAME` / `AUTH_PASSWORD` in
`.env` for anything beyond local testing), then open the **AWS Account**
panel and paste an access key, secret key, and — for temporary `ASIA*`
credentials — a session token for the account you want to analyse. Those
keys are held in memory for your login session only; nothing is written to
disk.

For a deployed (non-local) run, start uvicorn directly instead:

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

In that case the deployment account's credentials should come from an
instance profile or ECS task role rather than `.env`.

---

## Project structure

```
.
├── chat.py                  # Local launcher — opens browser, starts uvicorn
├── server.py                # FastAPI app: routes, SSE streaming, session CRUD
├── auth.py                  # Login sessions (single shared username/password)
├── credentials.py           # Per-login-session AWS keys, held in memory, STS-validated
├── aws_session.py           # boto3 client cache, keyed by (session, service, region)
├── deployment.py            # Health checks for the app's own Bedrock/STS credentials
├── chat_db.py                # chat_history.db — session list + message log for the UI
├── agents/
│   └── chat_agent.py        # build_chat_agent() — model, tools, system prompt
├── tools/
│   ├── aws_api.py           # call_aws_api() — allowlisted boto3 caller
│   └── python_repl.py       # execute_python() — sandboxed subprocess
├── static/
│   ├── index.html           # Chat + analysis UI (vanilla JS, no build step)
│   ├── login.html           # Sign-in page
│   └── deepagent-flow.html  # Rendered visual walkthrough of the agent loop
├── deploy/
│   ├── iam-policy-analysed-account.json   # Read-only role for accounts being scanned
│   └── iam-policy-deployment-account.json # Bedrock-invoke role for the app itself
├── tests/                   # pytest — auth, credentials, tool allowlist, sandbox isolation
├── requirements.txt
└── .env.example
```

`langgraph.db` (agent conversation checkpoints) and `chat_history.db`
(display history) are created automatically on first run and are not
committed to version control.

---

## Documentation

| Doc | In repo | Rendered artifact |
|---|---|---|
| Architecture — design thesis, request flow, trade-offs | [documentation/Architecture.md](documentation/Architecture.md) | [artifact](https://claude.ai/code/artifact/2cf83a8b-b32f-4fa9-8d79-6bfb3e4f1a71) |
| Technical — routes, SSE event schemas, persistence details | [documentation/TECHNICAL.md](documentation/TECHNICAL.md) | [artifact](https://claude.ai/code/artifact/32668ac9-7afe-4472-b48e-b9f2485c6e06) |
| Infra — deployment architecture, AWS resources, IAM, redeploy process | [documentation/Infra.md](documentation/Infra.md) | [artifact](https://claude.ai/code/artifact/6a0e75af-e05d-4b75-8153-5d276bde0eb7) |

Artifacts are private by default — share them from the artifact page's share menu if you want to hand a link to someone without repo access.

---

## Optional: LangSmith tracing

Add to `.env` to enable tracing of every agent call:

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your-langsmith-api-key
LANGCHAIN_PROJECT=cloud-cost-advisor
```
