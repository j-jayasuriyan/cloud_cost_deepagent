import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from tools.cost_calculator import estimate_s3_lifecycle_savings, S3_PRICE_PER_GB
from tools.aws_client import USE_LIVE, get_client, _REGION

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_s3.json").read_text())


# ── public tools ───────────────────────────────────────────────────────────────

def get_s3_inventory() -> str:
    """Return all S3 buckets with size, storage class distribution, lifecycle policy status, and cost."""
    return _live_s3_inventory() if USE_LIVE else json.dumps(_MOCK["buckets"], indent=2)


def analyze_s3_optimization() -> str:
    """
    Analyse S3 buckets: missing lifecycle policies, cold data in STANDARD class,
    versioning without expiry, cross-region replication using STANDARD at destination.
    """
    buckets = json.loads(_live_s3_inventory() if USE_LIVE else json.dumps(_MOCK["buckets"]))
    return _run_s3_analysis(buckets)


# ── shared analysis ────────────────────────────────────────────────────────────

def _run_s3_analysis(buckets: list) -> str:
    findings = []
    total_savings = 0.0

    for bucket in buckets:
        issues = []
        cold_pct = bucket.get("access_pattern", {}).get("objects_not_accessed_in_90_days_percent", 0)
        size_gb = bucket.get("total_size_gb", 0)

        if not bucket.get("lifecycle_policy_exists") and cold_pct > 50:
            savings = estimate_s3_lifecycle_savings(size_gb, cold_pct)
            issues.append({
                "type": "ADD_LIFECYCLE_POLICY",
                "detail": f"{cold_pct}% of objects not accessed in 90 days — transition to Glacier Instant Retrieval",
                **savings,
            })
            total_savings += savings["monthly_savings_usd"]

        if bucket.get("versioning_enabled") and not bucket.get("lifecycle_policy_exists"):
            issues.append({
                "type": "VERSION_ACCUMULATION",
                "detail": "Versioning enabled without lifecycle expiry — non-current versions accumulating",
                "recommendation": "Add lifecycle rule to expire non-current versions after 30 days",
            })

        if bucket.get("cross_region_replication") and not bucket.get("lifecycle_policy_exists"):
            dest = bucket.get("cross_region_replication_destination", "another region")
            savings_val = round(size_gb * 0.023 * 0.457, 2)
            issues.append({
                "type": "REPLICATION_STORAGE_CLASS",
                "detail": f"Cross-region replication to {dest} storing replicas in STANDARD — switch to STANDARD_IA",
                "estimated_monthly_savings_usd": savings_val,
            })
            total_savings += savings_val

        if issues:
            findings.append({
                "bucket_name": bucket["bucket_name"],
                "total_size_gb": size_gb,
                "current_monthly_cost_usd": bucket.get("monthly_cost_usd", 0),
                "issues": issues,
            })

    return json.dumps({"findings": findings, "total_estimated_monthly_savings_usd": round(total_savings, 2)}, indent=2)


# ── live implementation ────────────────────────────────────────────────────────

def _live_s3_inventory() -> str:
    s3 = get_client("s3")
    cw = get_client("cloudwatch")

    resp = s3.list_buckets()
    raw_buckets = resp.get("Buckets", [])
    buckets = []

    # CloudWatch S3 metrics are published daily — use last 2 days
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)

    for b in raw_buckets:
        name = b["Name"]
        creation = b.get("CreationDate", "").isoformat() if b.get("CreationDate") else ""

        # Bucket region
        try:
            loc = s3.get_bucket_location(Bucket=name)
            region = loc.get("LocationConstraint") or _REGION
        except Exception:
            region = "unknown"

        # Lifecycle policy
        lifecycle_exists = False
        try:
            s3.get_bucket_lifecycle_configuration(Bucket=name)
            lifecycle_exists = True
        except s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                pass  # permission error etc — assume no lifecycle

        # Versioning
        versioning_enabled = False
        try:
            ver = s3.get_bucket_versioning(Bucket=name)
            versioning_enabled = ver.get("Status") == "Enabled"
        except Exception:
            pass

        # Cross-region replication
        cross_region = False
        replication_dest = None
        try:
            rep = s3.get_bucket_replication(Bucket=name)
            rules = rep.get("ReplicationConfiguration", {}).get("Rules", [])
            if rules:
                cross_region = True
                dest_bucket = rules[0].get("Destination", {}).get("Bucket", "")
                replication_dest = dest_bucket.split(":")[-1] if ":" in dest_bucket else dest_bucket
        except Exception:
            pass

        # Size from CloudWatch (BucketSizeBytes — daily metric)
        total_size_gb = 0.0
        object_count = 0
        try:
            size_resp = cw.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[
                    {"Name": "BucketName", "Value": name},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
            if size_resp["Datapoints"]:
                total_size_gb = round(max(d["Average"] for d in size_resp["Datapoints"]) / (1024 ** 3), 2)

            count_resp = cw.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="NumberOfObjects",
                Dimensions=[
                    {"Name": "BucketName", "Value": name},
                    {"Name": "StorageType", "Value": "AllStorageTypes"},
                ],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
            if count_resp["Datapoints"]:
                object_count = int(max(d["Average"] for d in count_resp["Datapoints"]))
        except Exception:
            pass

        monthly_cost = round(total_size_gb * S3_PRICE_PER_GB["STANDARD"], 2)

        bucket_data = {
            "bucket_name": name,
            "region": region,
            "creation_date": creation,
            "total_size_gb": total_size_gb,
            "object_count": object_count,
            "storage_class_breakdown": {"STANDARD": total_size_gb},
            "lifecycle_policy_exists": lifecycle_exists,
            "versioning_enabled": versioning_enabled,
            "cross_region_replication": cross_region,
            "monthly_cost_usd": monthly_cost,
            "access_pattern": {
                "objects_not_accessed_in_90_days_percent": 0,
                "last_accessed_days_ago": 0,
            },
        }
        if replication_dest:
            bucket_data["cross_region_replication_destination"] = replication_dest

        buckets.append(bucket_data)

    return json.dumps(buckets, indent=2)
