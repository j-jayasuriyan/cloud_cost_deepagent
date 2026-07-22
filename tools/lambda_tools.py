import json
from pathlib import Path
from tools.cost_calculator import estimate_lambda_rightsize_savings
from tools.aws_client import USE_LIVE, get_client, get_cw_metrics_batch

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_lambda.json").read_text())

DEPRECATED_RUNTIMES = {"python3.9", "python3.8", "python3.7", "nodejs16.x", "nodejs14.x", "nodejs12.x"}


# ── public tools ───────────────────────────────────────────────────────────────

def get_lambda_inventory() -> str:
    """Return all Lambda functions with runtime, memory, timeout, cost, and usage metrics."""
    return _live_lambda_inventory() if USE_LIVE else json.dumps(_MOCK["functions"], indent=2)


def analyze_lambda_optimization() -> str:
    """
    Identify: over-provisioned memory, deprecated runtimes, idle functions,
    high error rates, and oversized timeouts.
    """
    functions = json.loads(_live_lambda_inventory() if USE_LIVE else json.dumps(_MOCK["functions"]))
    return _run_lambda_analysis(functions)


# ── shared analysis ────────────────────────────────────────────────────────────

def _run_lambda_analysis(functions: list) -> str:
    findings = []
    total_savings = 0.0

    for fn in functions:
        m = fn.get("metrics", {})
        issues = []
        current_mb = fn["memory_size_mb"]
        avg_used = m.get("avg_memory_used_mb", 0)

        if avg_used is not None and avg_used > 0 and avg_used < current_mb * 0.3:
            rs = estimate_lambda_rightsize_savings(current_mb, avg_used, fn["monthly_cost_usd"])
            if rs["monthly_savings_usd"] > 0:
                issues.append({"type": "MEMORY_OVERPROVISIONED",
                               "detail": f"Allocated {current_mb}MB, avg used only {avg_used}MB ({round(avg_used/current_mb*100,1)}%)",
                               **rs})
                total_savings += rs["monthly_savings_usd"]

        if fn.get("runtime") in DEPRECATED_RUNTIMES:
            issues.append({"type": "DEPRECATED_RUNTIME",
                           "detail": f"Runtime {fn['runtime']} is deprecated — upgrade required",
                           "recommended_action": "Upgrade to python3.12 or nodejs20.x"})

        if m.get("avg_invocations_per_month", 1) < 10:
            issues.append({"type": "IDLE_FUNCTION",
                           "detail": f"Only {m.get('avg_invocations_per_month', 0)} invocations/month",
                           "monthly_cost_usd": fn["monthly_cost_usd"],
                           "recommendation": "Review with owning team; delete if unused"})

        if m.get("error_rate_percent", 0) > 5:
            issues.append({"type": "HIGH_ERROR_RATE",
                           "detail": f"{m['error_rate_percent']}% error rate — compute wasted on failures",
                           "recommendation": "Investigate root cause"})

        max_dur_ms = m.get("max_duration_ms", 0)
        timeout_ms = fn["timeout_seconds"] * 1000
        if max_dur_ms > 0 and timeout_ms > max_dur_ms * 5:
            issues.append({"type": "TIMEOUT_OVERPROVISIONED",
                           "detail": f"Timeout {fn['timeout_seconds']}s, max observed {max_dur_ms}ms",
                           "recommendation": f"Reduce timeout to {round(max_dur_ms/1000*3)}s"})

        if issues:
            findings.append({
                "function_name": fn["function_name"],
                "runtime": fn.get("runtime", ""),
                "memory_mb": current_mb,
                "monthly_cost_usd": fn["monthly_cost_usd"],
                "issues": issues,
            })

    return json.dumps({"findings": findings, "total_estimated_monthly_savings_usd": round(total_savings, 2)}, indent=2)


# ── live implementation ────────────────────────────────────────────────────────

def _live_lambda_inventory() -> str:
    lam = get_client("lambda")
    functions = []
    paginator = lam.get_paginator("list_functions")
    raw_fns = []
    for page in paginator.paginate():
        raw_fns.extend(page["Functions"])

    # Batch CloudWatch queries: invocations, errors, duration per function
    queries = []
    id_map: dict = {}
    for idx, fn in enumerate(raw_fns):
        fname = fn["FunctionName"]
        dims = [{"Name": "FunctionName", "Value": fname}]
        ns = "AWS/Lambda"
        for metric, stat, short in [
            ("Invocations", "Sum", "inv"),
            ("Errors", "Sum", "err"),
            ("Duration", "Average", "dur"),
            ("Duration", "Maximum", "maxdur"),
            ("Throttles", "Sum", "thr"),
        ]:
            qid = f"lam{idx}{short}"
            queries.append({"id": qid, "namespace": ns, "metric_name": metric, "dimensions": dims, "stat": stat})
            id_map[qid] = (fname, short)

    raw_cw = get_cw_metrics_batch(queries, days=30)

    # Aggregate per function
    cw_by_fn: dict = {}
    for qid, val in raw_cw.items():
        fname, short = id_map[qid]
        cw_by_fn.setdefault(fname, {})
        cw_by_fn[fname][short] = val

    for fn in raw_fns:
        fname = fn["FunctionName"]
        cw = cw_by_fn.get(fname, {})
        mem_mb = fn.get("MemorySize", 128)
        timeout_s = fn.get("Timeout", 3)

        invocations = int(cw.get("inv", 0))
        errors = int(cw.get("err", 0))
        avg_dur_ms = round(cw.get("dur", 0), 2)
        max_dur_ms = round(cw.get("maxdur", 0), 2)
        throttles = int(cw.get("thr", 0))
        error_rate = round(errors / invocations * 100, 2) if invocations > 0 else 0.0

        # Lambda cost: $0.0000002/request + $0.0000166667/GB-second
        gb_seconds = (avg_dur_ms / 1000) * (mem_mb / 1024) * invocations
        monthly_cost = round(invocations * 0.0000002 + gb_seconds * 0.0000166667, 4)

        tags_resp = lam.list_tags(Resource=fn["FunctionArn"])
        tags = tags_resp.get("Tags", {})

        functions.append({
            "function_name": fname,
            "runtime": fn.get("Runtime", ""),
            "memory_size_mb": mem_mb,
            "timeout_seconds": timeout_s,
            "monthly_cost_usd": monthly_cost,
            "metrics": {
                "avg_invocations_per_month": invocations,
                "avg_duration_ms": avg_dur_ms,
                "max_duration_ms": max_dur_ms,
                "avg_memory_used_mb": None,  # requires Lambda Insights — not available in standard CW metrics
                "error_rate_percent": error_rate,
                "throttle_count_monthly": throttles,
            },
            "tags": tags,
        })

    return json.dumps(functions, indent=2)
