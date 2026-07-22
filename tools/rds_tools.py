import json
from pathlib import Path
from tools.aws_client import USE_LIVE, get_client, get_cw_metrics_batch

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_rds.json").read_text())

RDS_MONTHLY_PRICES_MULTI_AZ = {
    "db.t3.medium": {"mysql": 70.08, "postgres": 70.08},
    "db.m5.large": {"mysql": 192.24, "postgres": 192.24},
    "db.m5.xlarge": {"mysql": 384.48, "postgres": 384.48},
    "db.m5.2xlarge": {"mysql": 768.96, "postgres": 768.96},
    "db.r5.large": {"mysql": 240.48, "postgres": 240.48},
    "db.r5.xlarge": {"mysql": 480.96, "postgres": 480.96},
    "db.r5.2xlarge": {"mysql": 961.92, "postgres": 961.92},
    "db.r5.4xlarge": {"mysql": 1923.84, "postgres": 1923.84},
}

RDS_MONTHLY_PRICES_SINGLE_AZ = {k: {e: round(v / 2, 2) for e, v in v2.items()} for k, v2 in RDS_MONTHLY_PRICES_MULTI_AZ.items()}

RDS_DOWNSIZE_MAP = {
    "db.r5.4xlarge": "db.r5.2xlarge",
    "db.r5.2xlarge": "db.r5.xlarge",
    "db.m5.xlarge": "db.m5.large",
    "db.m5.2xlarge": "db.m5.xlarge",
}


# ── public tools ───────────────────────────────────────────────────────────────

def get_rds_inventory() -> str:
    """Return all RDS DB instances with engine, class, Multi-AZ status, and monthly cost."""
    return _live_rds_inventory() if USE_LIVE else json.dumps(_MOCK["db_instances"], indent=2)


def get_rds_reserved_instances() -> str:
    """Return active RDS Reserved Instances and their coverage."""
    return _live_rds_ris() if USE_LIVE else json.dumps(_MOCK["reserved_instances"], indent=2)


def analyze_rds_rightsizing() -> str:
    """
    Identify RDS instances that are underutilized, have unnecessary Multi-AZ in non-prod,
    or are running on-demand without RI coverage.
    """
    instances = json.loads(_live_rds_inventory() if USE_LIVE else json.dumps(_MOCK["db_instances"]))
    ris = json.loads(_live_rds_ris() if USE_LIVE else json.dumps(_MOCK["reserved_instances"]))
    return _run_rds_analysis(instances, ris)


# ── shared analysis ────────────────────────────────────────────────────────────

def _run_rds_analysis(instances: list, ris: list) -> str:
    findings = []
    total_savings = 0.0
    covered_classes = {ri["db_instance_class"] for ri in ris}

    for db in instances:
        m = db.get("cloudwatch_metrics", {})
        avg_cpu = m.get("avg_cpu_utilization_percent", 100)
        avg_conn = m.get("avg_database_connections", 999)
        env = db.get("tags", {}).get("Environment", "prod")
        finding = {"db_instance_id": db["db_instance_id"], "issues": [], "monthly_savings_usd": 0.0}

        if avg_cpu < 10 and avg_conn < 20:
            target = RDS_DOWNSIZE_MAP.get(db["db_instance_class"])
            if target:
                engine_key = "mysql" if "mysql" in db["engine"] else "postgres"
                prices = RDS_MONTHLY_PRICES_MULTI_AZ if db["multi_az"] else RDS_MONTHLY_PRICES_SINGLE_AZ
                new_cost = prices.get(target, {}).get(engine_key, db["monthly_cost_usd"] * 0.5)
                savings = round(db["monthly_cost_usd"] - new_cost, 2)
                finding["issues"].append({
                    "type": "RIGHTSIZE",
                    "detail": f"avg CPU {avg_cpu}%, avg connections {avg_conn} — downsize to {target}",
                    "recommended_class": target,
                    "monthly_savings_usd": savings,
                })
                finding["monthly_savings_usd"] += savings
                total_savings += savings

        if db["multi_az"] and env in ("staging", "dev"):
            savings = round(db["monthly_cost_usd"] * 0.5, 2)
            finding["issues"].append({
                "type": "DISABLE_MULTI_AZ",
                "detail": f"Multi-AZ on {env} — not required for non-prod",
                "monthly_savings_usd": savings,
            })
            finding["monthly_savings_usd"] += savings
            total_savings += savings

        if db["db_instance_class"] not in covered_classes and db.get("pricing_model") == "on-demand":
            finding["issues"].append({
                "type": "NO_RI_COVERAGE",
                "detail": "Running on-demand with no Reserved Instance — 1yr RI saves ~35-45%",
            })

        if finding["issues"]:
            findings.append(finding)

    return json.dumps({"findings": findings, "total_estimated_monthly_savings_usd": round(total_savings, 2)}, indent=2)


# ── live implementations ───────────────────────────────────────────────────────

def _live_rds_inventory() -> str:
    rds = get_client("rds")

    # Fetch active RIs upfront to determine pricing model per instance class
    active_ri_classes: set = set()
    try:
        ri_paginator = rds.get_paginator("describe_reserved_db_instances")
        for page in ri_paginator.paginate():
            for ri in page["ReservedDBInstances"]:
                if ri.get("State") == "active":
                    active_ri_classes.add(ri["DBInstanceClass"])
    except Exception:
        pass

    instances = []
    paginator = rds.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for db in page["DBInstances"]:
            engine = db.get("Engine", "")
            db_class = db.get("DBInstanceClass", "")
            multi_az = db.get("MultiAZ", False)
            engine_key = "mysql" if "mysql" in engine else "postgres"
            prices = RDS_MONTHLY_PRICES_MULTI_AZ if multi_az else RDS_MONTHLY_PRICES_SINGLE_AZ
            monthly_cost = prices.get(db_class, {}).get(engine_key, 0.0)
            pricing_model = "reserved" if db_class in active_ri_classes else "on-demand"
            tags = {t["Key"]: t["Value"] for t in db.get("TagList", [])}
            instances.append({
                "db_instance_id": db["DBInstanceIdentifier"],
                "db_instance_class": db_class,
                "engine": engine,
                "engine_version": db.get("EngineVersion", ""),
                "status": db.get("DBInstanceStatus", ""),
                "multi_az": multi_az,
                "storage_type": db.get("StorageType", "gp2"),
                "allocated_storage_gb": db.get("AllocatedStorage", 0),
                "monthly_cost_usd": monthly_cost,
                "pricing_model": pricing_model,
                "cloudwatch_metrics": {},
                "tags": tags,
            })

    # Batch CloudWatch for available instances
    available = [db for db in instances if db["status"] == "available"]
    if available:
        queries = []
        id_map: dict = {}
        for idx, db in enumerate(available):
            dbid = db["db_instance_id"]
            dims = [{"Name": "DBInstanceIdentifier", "Value": dbid}]
            for metric_name, stat, short in [
                ("CPUUtilization", "Average", "cpu"),
                ("DatabaseConnections", "Average", "conn"),
            ]:
                qid = f"rds{idx}{short}"
                queries.append({"id": qid, "namespace": "AWS/RDS", "metric_name": metric_name, "dimensions": dims, "stat": stat})
                id_map[qid] = (dbid, short)

        raw = get_cw_metrics_batch(queries)
        cw_by_db: dict = {}
        for qid, val in raw.items():
            dbid, short = id_map[qid]
            cw_by_db.setdefault(dbid, {})
            if short == "cpu":
                cw_by_db[dbid]["avg_cpu_utilization_percent"] = val
            elif short == "conn":
                cw_by_db[dbid]["avg_database_connections"] = int(val)

        for db in available:
            db["cloudwatch_metrics"] = cw_by_db.get(db["db_instance_id"], {})

    return json.dumps(instances, indent=2)


def _live_rds_ris() -> str:
    rds = get_client("rds")
    ris = []
    paginator = rds.get_paginator("describe_reserved_db_instances")
    for page in paginator.paginate():
        for ri in page["ReservedDBInstances"]:
            if ri.get("State") != "active":
                continue
            ris.append({
                "reserved_instance_id": ri["ReservedDBInstanceId"],
                "db_instance_class": ri["DBInstanceClass"],
                "engine": ri.get("ProductDescription", ""),
                "multi_az": ri.get("MultiAZ", False),
                "duration_years": round(ri.get("Duration", 31536000) / 31536000),
                "state": ri["State"],
                "monthly_savings_vs_ondemand_usd": 0.0,
            })
    return json.dumps(ris, indent=2)
