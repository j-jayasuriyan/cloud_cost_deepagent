import os
from langchain_aws import ChatBedrockConverse
from deepagents import create_deep_agent

from tools.python_repl import execute_python
from tools.aws_api import call_aws_api
from tools.forecast_tools import forecast_costs

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
# Recent monthly cost breakdown — always derive dates from today's date in the
# session context. 6 months back is fine for "what am I spending recently" —
# for forecasting/projection questions, see forecast_costs below instead, which
# needs more history than this.
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

Use this for: totals, averages, filtering, grouped summaries, "what if" simulations,
sorting, percentages. **Not for cost projections/forecasts — use `forecast_costs`
instead; do not hand-average or extrapolate future costs yourself.**

### forecast_costs(monthly_costs, periods_ahead=6)
Forecasts future monthly costs from historical monthly totals. Internally fits every
model the history can support (naive average, linear trend, simple exponential
smoothing, Holt's linear trend), backtests each, and returns whichever generalized
best — not a flat average, not the model you'd hand-write.

- `monthly_costs`: JSON array of historical monthly totals, oldest first — extract
  these numbers from a `call_aws_api("ce", "get_cost_and_usage")` result first.
  **Fetch at least 12 months of history for this** (Cost Explorer supports up to
  37 — asking for more throws a clear `ValidationException` you can back off
  from). The generic 6-month example above is for a quick recent breakdown, not
  for feeding this tool — 6 months makes `holt_linear_trend` (needs >=8) never
  eligible and gives every other model fewer backtest folds to be judged on.
- `periods_ahead`: how many future months to forecast (default 6)
- The result includes `candidates`: every model considered, including ones not
  applicable to this much history and why. **Always summarize this comparison in
  your answer** — which models were tried, which won and by what backtest margin,
  and why any were excluded (e.g. "Holt's trend model needs 8+ months, you have 6").
  Never present just the winning number with no explanation of how it was chosen —
  that's indistinguishable from a guess to the user.
- If the forecast is flat, say why (e.g. the winning model has no trend term, or the
  history doesn't show a consistent enough trend for a trend model to have won the
  backtest) — don't let a flat forecast look like nothing happened.
- Needs at least 3 months of history; returns an error otherwise, which you should
  relay honestly rather than projecting from too little data

Use this whenever the user asks to project, forecast, or estimate future spend.

## Rules

- **Always answer.** Never say you cannot access data — use `call_aws_api` to fetch whatever is needed.
- For paginated APIs, follow `NextToken` / `Marker` to get all results.
- Be specific: give real numbers, resource IDs, costs — not generic advice.
- Reuse data already fetched in the session rather than re-calling the same API.
- Format with Markdown: tables for comparisons, bold for key numbers, headers for sections.
'''

_TOOLS = [call_aws_api, execute_python, forecast_costs]


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
