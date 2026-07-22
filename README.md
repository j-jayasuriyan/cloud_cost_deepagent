# AWS Cloud Cost Optimization Advisor

An AI-powered multi-agent system that analyzes your AWS account for cost optimization opportunities across 8 services and produces prioritized reports in Markdown, HTML, and JSON.

Built with [DeepAgents](https://docs.langchain.com/oss/python/deepagents/overview) (LangChain/LangGraph) and Claude on Amazon Bedrock.

---

## Architecture

A master orchestrator delegates to 8 specialist sub-agents in parallel, then a report agent synthesizes everything:

```
Orchestrator
├── EC2 Analyst       — instance rightsizing, idle Elastic IPs
├── EBS Analyst       — unattached volumes, gp2→gp3, orphaned snapshots & AMIs
├── RDS Analyst       — DB rightsizing, Multi-AZ in non-prod, RI coverage gaps
├── S3 Analyst        — lifecycle policies, cold data, versioning, replication costs
├── Network Analyst   — idle load balancers, NAT Gateway waste, VPC endpoints
├── Savings Analyst   — Savings Plans, Reserved Instance coverage gaps
├── Lambda Analyst    — over-provisioned memory, deprecated runtimes, idle functions
├── CloudWatch Analyst— log group retention, orphaned metrics/alarms, unused dashboards
└── Report Agent      → report.json + report.html
```

---

## Prerequisites

- Python 3.10+
- AWS account with Bedrock access enabled for **Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`)
- IAM permissions for the services being analyzed (EC2, RDS, S3, CloudWatch, Cost Explorer, etc.)

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

Edit `.env` with your credentials:

```env
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_DEFAULT_REGION=us-east-1
```

---

## Running

**Mock mode** — uses bundled sample data, no AWS credentials needed:
```bash
python3 main.py
# or
python3 main.py --mock
```

**Live mode** — queries your real AWS account:
```bash
python3 main.py --live
```

**Debug mode** — prints raw stream chunk types (useful for troubleshooting):
```bash
python3 main.py --debug
```

---

## Output

Each run creates a timestamped folder under `output/`:

```
output/
  20260722_143512/
    report.md     ← full Markdown report
    report.html   ← styled dark-theme HTML
    report.json   ← structured findings per service
```

Previous runs are never overwritten.

---

## Optional: LangSmith Tracing

Add to `.env` to enable zero-code tracing of every agent call:

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=your-langsmith-api-key
LANGCHAIN_PROJECT=cloud-cost-advisor
```

---

## Project Structure

```
.
├── main.py                  # Entry point — CLI, streaming loop, output
├── config.py                # Bedrock model config
├── agents/
│   ├── orchestrator.py      # Master DeepAgent
│   ├── ec2_agent.py
│   ├── ebs_agent.py
│   ├── rds_agent.py
│   ├── s3_agent.py
│   ├── network_agent.py
│   ├── savings_agent.py
│   ├── lambda_agent.py
│   ├── cloudwatch_agent.py
│   └── report_agent.py
├── tools/
│   ├── aws_client.py        # boto3 client factory, CloudWatch batch helper
│   ├── ec2_tools.py
│   ├── ebs_tools.py
│   ├── rds_tools.py
│   ├── s3_tools.py
│   ├── network_tools.py
│   ├── lambda_tools.py
│   ├── cloudwatch_tools.py
│   ├── savings_tools.py
│   ├── report_tools.py      # JSON + HTML report writers
│   └── cost_calculator.py   # Shared pricing helpers
├── data/                    # Mock JSON fixtures (used in --mock mode)
├── requirements.txt
├── .env.example
└── .gitignore
```
