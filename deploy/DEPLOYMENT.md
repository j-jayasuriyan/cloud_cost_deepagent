# Deployment — EC2, single instance

Why single-instance: `auth.py` and `credentials.py` hold login sessions and
analysed-account AWS keys in an in-process dict, and `langgraph.db` /
`chat_history.db` are local SQLite files. None of that survives a second
instance or a process restart without extra work, so this guide deploys
exactly one long-running EC2 instance rather than an auto-scaling group.

If you outgrow that later (multiple instances, zero-downtime deploys), the
prerequisite work is: move sessions/credentials to Redis and swap SQLite for
a networked database — not something to bolt on after the fact.

---

## 0. Prerequisites

- AWS CLI configured with permissions to create IAM roles and EC2 instances
- **Bedrock model access enabled** for `us.anthropic.claude-haiku-4-5-20251001-v1:0`
  in your target region — Bedrock console → Model access → request it. This is
  separate from IAM and is the single most common reason `/deployment` reports
  unhealthy after everything else is set up.
- A domain name, if you want TLS via ACM (recommended — see step 6)

---

## 1. IAM role for the instance

The instance needs credentials to call STS and Bedrock — never put deployment
credentials in `.env` on a real deployment; use an instance profile so
nothing sits on disk.

```bash
cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "ec2.amazonaws.com"},
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name cost-advisor-deployment \
  --assume-role-policy-document file://trust-policy.json

aws iam create-instance-profile \
  --instance-profile-name cost-advisor-deployment

aws iam add-role-to-instance-profile \
  --instance-profile-name cost-advisor-deployment \
  --role-name cost-advisor-deployment
```

Attach the permissions from [`iam-policy-deployment-account.json`](iam-policy-deployment-account.json)
after substituting `REGION` and `DEPLOYMENT_ACCOUNT_ID` (`aws sts get-caller-identity`
gives you the account ID):

```bash
sed -e "s/REGION/us-east-1/g" -e "s/DEPLOYMENT_ACCOUNT_ID/123456789012/g" \
  iam-policy-deployment-account.json > iam-policy-deployment-account.filled.json

aws iam put-role-policy \
  --role-name cost-advisor-deployment \
  --policy-name bedrock-and-sts \
  --policy-document file://iam-policy-deployment-account.filled.json
```

> An earlier version of that file also granted `secretsmanager:GetSecretValue`
> on `cost-advisor/analysed-account-*`. It's been removed — nothing in the
> codebase reads Secrets Manager (analysed-account credentials only ever come
> in through the UI, via `credentials.py`, and stay in memory), and
> `tools/aws_api.py` blocks the `secretsmanager` service outright. The grant
> was pure unused attack surface. Add it back only if you're deliberately
> building a Secrets-Manager-backed credential path.

---

## 2. Launch the instance

- AMI: Amazon Linux 2023 (commands below assume it; adjust package manager and
  default user for Ubuntu)
- Instance type: `t3.small` is comfortably enough for one FastAPI process +
  two SQLite files; `t3.micro` works for light/personal use
- IAM instance profile: `cost-advisor-deployment`
- Security group: allow inbound `22` (SSH, restricted to your IP) and either
  `8000` (if fronting with an ALB — step 6a) or `443`/`80` (if terminating TLS
  on-box — step 6b). Don't expose 8000 to the internet directly; `COOKIE_SECURE`
  defaults to `true`, so a plain-HTTP client can't even get a working session.
- Root volume: default 8–20 GiB is plenty. This volume is what makes chat
  history and agent memory durable across reboots — see step 7 on backups for
  what it does *not* protect against.

---

## 3. Install the app

SSH in, then:

```bash
sudo dnf install -y python3.12 python3.12-venv git   # Ubuntu: apt install python3-venv git

sudo mkdir -p /opt/cost-advisor
sudo chown $USER:$USER /opt/cost-advisor
git clone <repo-url> /opt/cost-advisor
cd /opt/cost-advisor

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`. On an instance with the profile from step 1, leave
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` **unset** —
boto3 picks up the instance profile automatically, which is the whole point
of using one. Set:

```env
AWS_DEFAULT_REGION=us-east-1
AUTH_USERNAME=<pick something that isn't Admin>
AUTH_PASSWORD=<pick something that isn't Admin@123>
COOKIE_SECURE=true
```

---

## 4. Run it as a service

Install the unit shipped alongside this guide:

```bash
sudo cp deploy/cost-advisor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cost-advisor
sudo systemctl status cost-advisor
```

`server.py` loads `.env` itself via `python-dotenv` at import time (resolved
relative to its own file location), so the unit doesn't need an
`EnvironmentFile` directive — it's already covered as long as `.env` sits
next to `server.py`.

Logs: `journalctl -u cost-advisor -f`

---

## 5. Smoke test

```bash
curl -s localhost:8000/health          # {"status": "ok"}
curl -s localhost:8000/deployment      # {"ok": true, "account_id": "...", ...}
```

If `/deployment` reports `ok: false`, it's almost always the Bedrock model
access step (§0) or the IAM policy substitution (§1), not the app itself —
`deployment.py` checks STS and a real `bedrock-runtime.converse()` call, not
just that credentials parse.

---

## 6. Put TLS in front

Required in practice: `COOKIE_SECURE=true` means the login cookie is refused
over plain HTTP, so the app is unusable without TLS from outside the
instance.

**6a. ALB + ACM (recommended)** — AWS manages certificate renewal, no
certbot cron job to babysit.

- Request/validate a certificate in ACM for your domain
- Create a target group pointing at this one instance, port `8000`,
  health check path `/health`
- Create an ALB with an HTTPS (443) listener using the ACM cert, forwarding
  to that target group
- Security group on the instance: allow port `8000` only from the ALB's
  security group

**6b. nginx + certbot on-box** — cheaper (no ALB hourly cost), more to
maintain yourself.

```nginx
server {
    listen 443 ssl;
    server_name your-domain.example;
    # certbot fills in ssl_certificate / ssl_certificate_key

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;              # required for SSE (/chat, /analysis/run)
        proxy_read_timeout 300s;          # matches the model client's own timeout
    }
}
```

`proxy_buffering off` matters specifically here — without it nginx buffers
the whole SSE response before forwarding, and the chat/analysis panels stop
streaming and just hang until the agent finishes.

---

## 7. Operations

**Backups.** The two SQLite files are the only state that matters:
`langgraph.db` (agent memory) and `chat_history.db` (sidebar history). An
EBS snapshot schedule (Data Lifecycle Manager, daily) covers both. Losing the
instance without a recent snapshot means losing all chat history — nothing
else in the app is stateful.

**Deploying an update.**
```bash
cd /opt/cost-advisor
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart cost-advisor
```
This is a hard cutover — in-flight SSE streams get dropped. There's no
rolling-restart option with a single instance and no load balancer routing
around it.

**Rotating the login password.** Change `AUTH_PASSWORD` in `.env` and
`systemctl restart cost-advisor`. Every existing session cookie is
invalidated on restart anyway (`auth._sessions` is in-memory), so there's no
separate revocation step.
