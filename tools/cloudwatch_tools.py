import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from tools.aws_client import USE_LIVE, get_client

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_cloudwatch.json").read_text())

RECOMMENDED_RETENTION_DAYS = {
    "/dev/": 7,
    "/aws/lambda/": 30,
    "/aws/rds/": 30,
    "/ecs/": 60,
    "/aws/apigateway/": 30,
    "default": 90,
}


# ── public tools ───────────────────────────────────────────────────────────────

def get_cloudwatch_log_groups() -> str:
    """Return all CloudWatch Log Groups with retention settings, stored size, and monthly cost."""
    return _live_log_groups() if USE_LIVE else json.dumps(_MOCK["log_groups"], indent=2)


def get_cloudwatch_custom_metrics() -> str:
    """Return custom CloudWatch metric namespaces and their monthly cost."""
    return _live_custom_metrics() if USE_LIVE else json.dumps(_MOCK["custom_metrics"], indent=2)


def get_cloudwatch_alarms() -> str:
    """Return CloudWatch alarms, flagging any in INSUFFICIENT_DATA with no backing metric."""
    return _live_alarms() if USE_LIVE else json.dumps(_MOCK["alarms"], indent=2)


def analyze_cloudwatch_optimization() -> str:
    """
    Identify: log groups without retention policy, orphaned custom metrics,
    orphaned alarms (INSUFFICIENT_DATA with missing metric), and unused dashboards.
    """
    log_groups = json.loads(_live_log_groups() if USE_LIVE else json.dumps(_MOCK["log_groups"]))
    custom_metrics = json.loads(_live_custom_metrics() if USE_LIVE else json.dumps(_MOCK["custom_metrics"]))
    alarms = json.loads(_live_alarms() if USE_LIVE else json.dumps(_MOCK["alarms"]))
    dashboards = _live_dashboards() if USE_LIVE else _MOCK.get("dashboards", [])
    return _run_cw_analysis(log_groups, custom_metrics, alarms, dashboards)


# ── shared analysis ────────────────────────────────────────────────────────────

def _run_cw_analysis(log_groups, custom_metrics, alarms, dashboards) -> str:
    findings = {"no_retention_policy": [], "orphaned_metrics": [], "orphaned_alarms": [], "unused_dashboards": []}
    total_savings = 0.0

    for lg in log_groups:
        if lg["retention_days"] is None:
            recommended = RECOMMENDED_RETENTION_DAYS["default"]
            for prefix, days in RECOMMENDED_RETENTION_DAYS.items():
                if prefix in lg["log_group_name"]:
                    recommended = days
                    break
            current_cost = lg["monthly_storage_cost_usd"]
            new_cost = round(current_cost * (recommended / 365), 2)
            savings = round(current_cost - new_cost, 2)
            findings["no_retention_policy"].append({
                "log_group": lg["log_group_name"],
                "stored_gb": lg["stored_gb"],
                "current_monthly_storage_cost_usd": current_cost,
                "recommended_retention_days": recommended,
                "estimated_monthly_savings_usd": savings,
            })
            total_savings += savings

    for metric in custom_metrics:
        if metric.get("last_data_point_days_ago", 0) > 60:
            findings["orphaned_metrics"].append({
                "namespace": metric["namespace"],
                "metric_count": metric["metric_count"],
                "last_data_point_days_ago": metric["last_data_point_days_ago"],
                "monthly_cost_usd": metric["monthly_cost_usd"],
                "recommendation": "DELETE — no data in >60 days",
            })
            total_savings += metric["monthly_cost_usd"]

    for alarm in alarms:
        if not alarm.get("metric_exists") and alarm.get("state") == "INSUFFICIENT_DATA":
            findings["orphaned_alarms"].append({
                "alarm_name": alarm["alarm_name"],
                "state": alarm["state"],
                "monthly_cost_usd": alarm["monthly_cost_usd"],
                "recommendation": "DELETE — backing metric no longer exists",
            })
            total_savings += alarm["monthly_cost_usd"]

    for dashboard in dashboards:
        last_viewed = dashboard.get("last_viewed_days_ago")
        if last_viewed is not None and last_viewed > 90:
            findings["unused_dashboards"].append({
                "dashboard_name": dashboard["dashboard_name"],
                "last_viewed_days_ago": dashboard["last_viewed_days_ago"],
                "monthly_cost_usd": dashboard["monthly_cost_usd"],
                "recommendation": "DELETE — not viewed in >90 days",
            })
            total_savings += dashboard["monthly_cost_usd"]

    findings["total_estimated_monthly_savings_usd"] = round(total_savings, 2)
    return json.dumps(findings, indent=2)


# ── live implementations ───────────────────────────────────────────────────────

def _live_log_groups() -> str:
    logs = get_client("logs")
    log_groups = []
    paginator = logs.get_paginator("describe_log_groups")
    for page in paginator.paginate():
        for lg in page["logGroups"]:
            stored_bytes = lg.get("storedBytes", 0)
            stored_gb = round(stored_bytes / (1024 ** 3), 2)
            monthly_storage_cost = round(stored_gb * 0.03, 2)  # $0.03/GB/month

            # Approximate monthly ingestion from stored bytes trend (rough estimate)
            monthly_ingestion_gb = round(stored_gb * 0.05, 2)  # assume 5% of stored = monthly ingestion
            monthly_ingestion_cost = round(monthly_ingestion_gb * 0.50, 2)  # $0.50/GB ingested

            log_groups.append({
                "log_group_name": lg["logGroupName"],
                "retention_days": lg.get("retentionInDays"),
                "stored_bytes": stored_bytes,
                "stored_gb": stored_gb,
                "monthly_storage_cost_usd": monthly_storage_cost,
                "monthly_ingestion_gb": monthly_ingestion_gb,
                "monthly_ingestion_cost_usd": monthly_ingestion_cost,
            })
    return json.dumps(log_groups, indent=2)


def _live_custom_metrics() -> str:
    cw = get_client("cloudwatch")
    metrics_resp = cw.list_metrics()
    namespaces: dict = {}
    aws_namespaces = {"AWS/", "CWAgent"}

    for m in metrics_resp.get("Metrics", []):
        ns = m["Namespace"]
        if any(ns.startswith(prefix) for prefix in aws_namespaces):
            continue  # skip built-in AWS namespaces
        if ns not in namespaces:
            namespaces[ns] = 0
        namespaces[ns] += 1

    # Check last data point for each custom namespace
    end = datetime.now(timezone.utc)
    result = []
    for ns, count in namespaces.items():
        last_days_ago = 0
        try:
            resp = cw.get_metric_statistics(
                Namespace=ns,
                MetricName=next(m["MetricName"] for m in metrics_resp["Metrics"] if m["Namespace"] == ns),
                Dimensions=[],
                StartTime=end - timedelta(days=90),
                EndTime=end,
                Period=86400,
                Statistics=["SampleCount"],
            )
            if resp["Datapoints"]:
                latest = max(d["Timestamp"] for d in resp["Datapoints"])
                last_days_ago = (end - latest).days
            else:
                last_days_ago = 90
        except Exception:
            last_days_ago = 0

        result.append({
            "namespace": ns,
            "metric_count": count,
            "monthly_cost_usd": round(count * 0.30, 2),  # $0.30/metric/month
            "last_data_point_days_ago": last_days_ago,
            "description": f"Custom metrics in namespace {ns}",
        })

    return json.dumps(result, indent=2)


def _live_alarms() -> str:
    cw = get_client("cloudwatch")
    alarms = []
    paginator = cw.get_paginator("describe_alarms")
    for page in paginator.paginate():
        for alarm in page["MetricAlarms"]:
            # An alarm has no backing metric if it's in INSUFFICIENT_DATA and the metric doesn't exist
            state = alarm.get("StateValue", "OK")
            metric_exists = True
            if state == "INSUFFICIENT_DATA":
                try:
                    resp = cw.list_metrics(
                        Namespace=alarm.get("Namespace", ""),
                        MetricName=alarm.get("MetricName", ""),
                        Dimensions=alarm.get("Dimensions", []),
                    )
                    metric_exists = len(resp.get("Metrics", [])) > 0
                except Exception:
                    metric_exists = True

            alarms.append({
                "alarm_name": alarm["AlarmName"],
                "state": state,
                "monthly_cost_usd": 0.10,  # $0.10/alarm/month
                "metric_exists": metric_exists,
            })
    return json.dumps(alarms, indent=2)


def _live_dashboards() -> list:
    cw = get_client("cloudwatch")
    dashboards = []
    try:
        resp = cw.list_dashboards()
        for d in resp.get("DashboardEntries", []):
            dashboards.append({
                "dashboard_name": d["DashboardName"],
                "monthly_cost_usd": 3.00,
                "last_viewed_days_ago": None,  # CloudWatch API does not expose last-viewed time
            })
    except Exception:
        pass
    return dashboards
