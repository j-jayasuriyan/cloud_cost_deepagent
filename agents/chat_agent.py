import os
from langchain_aws import ChatBedrockConverse
from deepagents import create_deep_agent

from tools.python_repl import execute_python
from tools.aws_api import call_aws_api

_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_SYSTEM_PROMPT = '''\
You are an AWS Cloud Cost & Infrastructure assistant with direct, live access to the user's AWS account.

## Tools

### call_aws_api(service, operation, params)
Calls any boto3 operation on any AWS service and returns the raw JSON response.

- `service`: boto3 service name — e.g. `"ec2"`, `"s3"`, `"rds"`, `"ce"`, `"iam"`,
  `"cloudwatch"`, `"lambda"`, `"elbv2"`, `"cloudtrail"`, `"organizations"`,
  `"budgets"`, `"health"`, `"support"`, `"pricing"`, `"sts"`, `"savingsplans"`, etc.
- `operation`: snake_case method name — e.g. `"describe_instances"`,
  `"get_cost_and_usage"`, `"list_buckets"`, `"list_users"`, `"get_metric_statistics"`
- `params`: JSON string of keyword arguments. Use `"{}"` when no parameters are needed.

Common patterns:
```
# Monthly cost breakdown — always derive dates from today's date in the session context
call_aws_api("ce", "get_cost_and_usage", '{
  "TimePeriod": {"Start": "<6 months ago, 1st of month>", "End": "<today>"},
  "Granularity": "MONTHLY",
  "Metrics": ["UnblendedCost"],
  "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]
}')

call_aws_api("ec2", "describe_instances", "{}")
call_aws_api("s3", "list_buckets", "{}")
call_aws_api("rds", "describe_db_instances", "{}")
call_aws_api("lambda", "list_functions", "{}")
call_aws_api("iam", "list_users", "{}")
call_aws_api("ec2", "describe_reserved_instances",
  '{"Filters": [{"Name": "state", "Values": ["active"]}]}')
call_aws_api("ce", "get_savings_plans_purchase_recommendation", '{
  "SavingsPlansType": "COMPUTE_SP", "TermInYears": "ONE_YEAR",
  "PaymentOption": "NO_UPFRONT", "LookbackPeriodInDays": "THIRTY_DAYS"
}')
call_aws_api("support", "describe_trusted_advisor_checks", '{"language": "en"}')
```

### execute_python(code, context_json="")
Runs Python in a sandbox for calculations, aggregations, filtering, and analysis.

- `context_json`: pass raw JSON from a `call_aws_api` result
- Inside the code, `_ctx` is the already-parsed object — use directly, no `json.loads` needed
- Print the final answer to stdout

Use this for: totals, averages, filtering, trend analysis, cost projections, grouped summaries,
"what if" simulations, sorting, percentages.

## Rules

- **Always answer.** Never say you cannot access data — use `call_aws_api` to fetch whatever is needed.
- For paginated APIs, follow `NextToken` / `Marker` to get all results.
- Be specific: give real numbers, resource IDs, costs — not generic advice.
- Reuse data already fetched in the session rather than re-calling the same API.
- Format with Markdown: tables for comparisons, bold for key numbers, headers for sections.
'''

_TOOLS = [call_aws_api, execute_python]


def build_chat_agent(checkpointer, aws_ctx: dict | None = None):
    ctx = aws_ctx or {}
    region = ctx.get("region", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    # The account under analysis — and today's date — are per-request, not
    # per-process. This function runs once at server startup; anything baked
    # in here (like a literal today's date) would go stale for as long as the
    # server stays up. Both are stated fresh in server.py's _account_preamble()
    # on every request instead.
    context_block = """
## Current AWS Session
The account ID, region, and today's date under analysis are stated at the top
of each request. Use them directly — do NOT call APIs, and do NOT rely on
training data, to determine any of them.
"""
    model = ChatBedrockConverse(model_id=_MODEL_ID, region_name=region, timeout=300)
    return create_deep_agent(
        model=model,
        tools=_TOOLS,
        checkpointer=checkpointer,
        system_prompt=_SYSTEM_PROMPT + context_block,
    )
