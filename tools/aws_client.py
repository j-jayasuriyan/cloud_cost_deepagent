"""
Shared boto3 client factory and CloudWatch batch helpers.
USE_LIVE is set by main.py via AWS_DATA_MODE env var before any tool is imported.
"""

import os
import boto3
from botocore.config import Config
from datetime import datetime, timedelta, timezone

USE_LIVE: bool = os.environ.get("AWS_DATA_MODE", "mock").lower() == "live"
_REGION: str = os.environ.get("AWS_DEFAULT_REGION", "")

# Timeouts: 10s connect, 30s read; retry transient errors up to 3 times
_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"max_attempts": 3, "mode": "adaptive"},
)


def get_client(service: str):
    return boto3.client(service, region_name=_REGION, config=_BOTO_CONFIG)


def get_account_id() -> str:
    try:
        return get_client("sts").get_caller_identity()["Account"]
    except Exception:
        return "unknown"


def get_cw_metrics_batch(metric_queries: list, days: int = 30) -> dict:
    """
    Batch CloudWatch GetMetricData call.

    metric_queries: list of {id, namespace, metric_name, dimensions, stat}
      - id must start with a lowercase letter and contain only [a-zA-Z0-9_]
      - stat: "Average" | "Maximum" | "Sum" | "SampleCount"

    Returns {id: aggregated_float}
      - For "Average" queries  → mean of daily datapoints
      - For "Maximum" queries  → max of daily datapoints
      - For "Sum" queries      → sum of daily datapoints
    """
    cw = get_client("cloudwatch")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    results: dict = {}

    for batch_start in range(0, len(metric_queries), 500):
        batch = metric_queries[batch_start : batch_start + 500]
        cw_queries = [
            {
                "Id": q["id"],
                "MetricStat": {
                    "Metric": {
                        "Namespace": q["namespace"],
                        "MetricName": q["metric_name"],
                        "Dimensions": q["dimensions"],
                    },
                    "Period": 86400,
                    "Stat": q.get("stat", "Average"),
                },
                "ReturnData": True,
            }
            for q in batch
        ]
        try:
            resp = cw.get_metric_data(
                MetricDataQueries=cw_queries,
                StartTime=start,
                EndTime=end,
            )
            for r in resp.get("MetricDataResults", []):
                values = r.get("Values", [])
                if not values:
                    results[r["Id"]] = 0.0
                else:
                    stat = next(
                        (q.get("stat", "Average") for q in batch if q["id"] == r["Id"]),
                        "Average",
                    )
                    if stat == "Maximum":
                        results[r["Id"]] = round(max(values), 4)
                    elif stat == "Sum":
                        results[r["Id"]] = round(sum(values), 4)
                    else:
                        results[r["Id"]] = round(sum(values) / len(values), 4)
        except Exception:
            for q in batch:
                results[q["id"]] = 0.0

    return results


def safe_metric_id(raw: str) -> str:
    """Sanitize a string into a valid CloudWatch metric query Id."""
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in raw)
    if cleaned and cleaned[0].isdigit():
        cleaned = "m" + cleaned
    return cleaned or "m0"
