import json
from pathlib import Path
from tools.cost_calculator import estimate_ec2_rightsize_savings, EC2_MONTHLY_PRICES
from tools.aws_client import USE_LIVE, get_client, get_cw_metrics_batch

# ── mock data ──────────────────────────────────────────────────────────────────
_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_ec2.json").read_text())

# module-level cache for live mode so analyze_ec2_rightsizing can reuse inventory
_live_cache: dict = {}


# ── public tools ───────────────────────────────────────────────────────────────

def get_ec2_inventory() -> str:
    """Return all EC2 instances with state, type, cost, and CloudWatch CPU metrics."""
    return _live_ec2_inventory() if USE_LIVE else json.dumps(_MOCK["instances"], indent=2)


def get_elastic_ips() -> str:
    """Return all Elastic IP allocations, flagging unassociated ones that incur charges."""
    return _live_elastic_ips() if USE_LIVE else json.dumps(_MOCK["elastic_ips"], indent=2)


def analyze_ec2_rightsizing(instance_id: str) -> str:
    """
    Given an instance ID, return rightsizing recommendation based on CPU utilization.
    Flags instances with avg CPU < 10% as candidates for downsizing.
    """
    instances = (
        _live_cache.get("instances", [])
        if USE_LIVE
        else _MOCK["instances"]
    )
    instance = next((i for i in instances if i["instance_id"] == instance_id), None)
    if not instance:
        return json.dumps({"error": f"Instance {instance_id} not found"})

    metrics = instance.get("cloudwatch_metrics", {})
    avg_cpu = metrics.get("avg_cpu_utilization_percent", 0)
    result = {
        "instance_id": instance_id,
        "name": instance["name"],
        "instance_type": instance["instance_type"],
        "avg_cpu_percent": avg_cpu,
        "monthly_cost_usd": instance["monthly_cost_usd"],
        "recommendation": None,
        "savings": None,
    }

    if instance["state"] == "stopped":
        result["recommendation"] = "TERMINATE_OR_SNAPSHOT — stopped instance still has attached EBS costs"
    elif avg_cpu < 5.0:
        savings = estimate_ec2_rightsize_savings(instance["instance_type"], instance["monthly_cost_usd"])
        result["recommendation"] = "RIGHTSIZE — severely underutilized (avg CPU < 5%)"
        result["savings"] = savings
    elif avg_cpu < 10.0:
        savings = estimate_ec2_rightsize_savings(instance["instance_type"], instance["monthly_cost_usd"])
        result["recommendation"] = "RIGHTSIZE — underutilized (avg CPU < 10%)"
        result["savings"] = savings
    else:
        result["recommendation"] = "OK — utilization is acceptable"

    return json.dumps(result, indent=2)


# ── live implementations ───────────────────────────────────────────────────────

def _live_ec2_inventory() -> str:
    ec2 = get_client("ec2")
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                itype = inst.get("InstanceType", "")
                instances.append({
                    "instance_id": inst["InstanceId"],
                    "instance_type": itype,
                    "state": inst["State"]["Name"],
                    "name": tags.get("Name", inst["InstanceId"]),
                    "launch_time": inst.get("LaunchTime", "").isoformat() if inst.get("LaunchTime") else "",
                    "availability_zone": inst["Placement"]["AvailabilityZone"],
                    "platform": "windows" if inst.get("Platform") == "windows" else "linux",
                    "pricing_model": "spot" if inst.get("InstanceLifecycle") == "spot" else "on-demand",
                    "monthly_cost_usd": EC2_MONTHLY_PRICES.get(itype, 0.0),
                    "cloudwatch_metrics": {},
                    "tags": tags,
                })

    # Batch CloudWatch CPU queries for running instances
    running = [i for i in instances if i["state"] == "running"]
    if running:
        queries = []
        id_map: dict = {}
        for idx, inst in enumerate(running):
            iid = inst["instance_id"]
            avg_id = f"ec2avg{idx}"
            max_id = f"ec2max{idx}"
            id_map[avg_id] = (iid, "avg")
            id_map[max_id] = (iid, "max")
            dims = [{"Name": "InstanceId", "Value": iid}]
            queries += [
                {"id": avg_id, "namespace": "AWS/EC2", "metric_name": "CPUUtilization", "dimensions": dims, "stat": "Average"},
                {"id": max_id, "namespace": "AWS/EC2", "metric_name": "CPUUtilization", "dimensions": dims, "stat": "Maximum"},
            ]

        raw = get_cw_metrics_batch(queries)
        cw_by_id: dict = {}
        for qid, val in raw.items():
            iid, stat = id_map[qid]
            cw_by_id.setdefault(iid, {})
            if stat == "avg":
                cw_by_id[iid]["avg_cpu_utilization_percent"] = val
            else:
                cw_by_id[iid]["max_cpu_utilization_percent"] = val

        for inst in running:
            inst["cloudwatch_metrics"] = cw_by_id.get(inst["instance_id"], {})

    _live_cache["instances"] = instances
    return json.dumps(instances, indent=2)


def _live_elastic_ips() -> str:
    ec2 = get_client("ec2")
    resp = ec2.describe_addresses()
    eips = []
    for addr in resp.get("Addresses", []):
        associated = addr.get("AssociationId") is not None
        eips.append({
            "allocation_id": addr.get("AllocationId", ""),
            "public_ip": addr.get("PublicIp", ""),
            "association_id": addr.get("AssociationId"),
            "name": next((t["Value"] for t in addr.get("Tags", []) if t["Key"] == "Name"), addr.get("PublicIp", "")),
            "monthly_cost_usd": 0.0 if associated else 3.65,
        })
    return json.dumps(eips, indent=2)
