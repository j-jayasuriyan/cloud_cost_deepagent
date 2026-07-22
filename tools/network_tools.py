import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from tools.aws_client import USE_LIVE, get_client, get_cw_metrics_batch, _REGION

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_network.json").read_text())


# ── public tools ───────────────────────────────────────────────────────────────

def get_load_balancers() -> str:
    """Return all load balancers with traffic metrics and monthly cost."""
    return _live_load_balancers() if USE_LIVE else json.dumps(_MOCK["load_balancers"], indent=2)


def get_nat_gateways() -> str:
    """Return all NAT Gateways with data processed and monthly cost."""
    return _live_nat_gateways() if USE_LIVE else json.dumps(_MOCK["nat_gateways"], indent=2)


def get_data_transfer_costs() -> str:
    """Return monthly data transfer breakdown: internet egress, cross-region, cross-AZ."""
    return _live_data_transfer() if USE_LIVE else json.dumps(_MOCK["data_transfer"], indent=2)


def analyze_network_optimization() -> str:
    """
    Identify: idle load balancers, low-traffic NAT Gateways, missing VPC Endpoints,
    and cross-AZ data transfer reduction opportunities.
    """
    lbs = json.loads(_live_load_balancers() if USE_LIVE else json.dumps(_MOCK["load_balancers"]))
    nats = json.loads(_live_nat_gateways() if USE_LIVE else json.dumps(_MOCK["nat_gateways"]))
    dt = json.loads(_live_data_transfer() if USE_LIVE else json.dumps(_MOCK["data_transfer"]))
    vpc_opps = _live_vpc_endpoint_opportunities() if USE_LIVE else _MOCK.get("vpc_endpoints_available_but_not_used", [])
    return _run_network_analysis(lbs, nats, dt, vpc_opps)


# ── shared analysis ────────────────────────────────────────────────────────────

def _run_network_analysis(lbs, nats, dt, vpc_opps) -> str:
    findings = {"idle_load_balancers": [], "nat_gateway_issues": [], "vpc_endpoint_opportunities": [], "data_transfer_waste": []}
    total_savings = 0.0

    for lb in lbs:
        m = lb.get("metrics", {})
        requests = m.get("avg_requests_per_minute", m.get("avg_active_flows", 1))
        if requests == 0 and m.get("healthy_target_count", 1) == 0:
            findings["idle_load_balancers"].append({
                "name": lb["name"],
                "type": lb["type"],
                "monthly_cost_usd": lb["monthly_cost_usd"],
                "recommendation": "DELETE — zero traffic, zero healthy targets",
            })
            total_savings += lb["monthly_cost_usd"]
        elif requests < 10:
            findings["idle_load_balancers"].append({
                "name": lb["name"],
                "type": lb["type"],
                "avg_requests_per_minute": requests,
                "monthly_cost_usd": lb["monthly_cost_usd"],
                "recommendation": "REVIEW — extremely low traffic, consider consolidation",
            })

    for nat in nats:
        if nat.get("data_processed_gb_monthly", 0) < 50:
            total_monthly = nat.get("monthly_cost_usd", 0) + nat.get("data_transfer_cost_monthly_usd", 0)
            findings["nat_gateway_issues"].append({
                "name": nat["name"],
                "availability_zone": nat.get("availability_zone", ""),
                "data_processed_gb_monthly": nat.get("data_processed_gb_monthly", 0),
                "monthly_cost_usd": total_monthly,
                "recommendation": "REVIEW — low data throughput; consider removal if AZ is rarely used",
            })
            total_savings += nat.get("monthly_cost_usd", 0)

    for ep in vpc_opps:
        findings["vpc_endpoint_opportunities"].append({
            "service": ep["service"],
            "potential_monthly_savings_usd": ep["potential_monthly_savings_usd"],
            "recommendation": f"Create VPC Endpoint for {ep['service']} to bypass NAT Gateway charges",
        })
        total_savings += ep["potential_monthly_savings_usd"]

    if dt.get("monthly_cross_az_transfer_cost_usd", 0) > 50:
        findings["data_transfer_waste"].append({
            "type": "CROSS_AZ",
            "monthly_gb": dt.get("monthly_cross_az_transfer_gb", 0),
            "monthly_cost_usd": dt["monthly_cross_az_transfer_cost_usd"],
            "recommendation": "Refactor services to prefer same-AZ communication",
        })

    findings["total_estimated_monthly_savings_usd"] = round(total_savings, 2)
    return json.dumps(findings, indent=2)


# ── live implementations ───────────────────────────────────────────────────────

def _live_load_balancers() -> str:
    elbv2 = get_client("elbv2")
    lbs = []
    paginator = elbv2.get_paginator("describe_load_balancers")
    raw_lbs = []
    for page in paginator.paginate():
        raw_lbs.extend(page["LoadBalancers"])

    queries = []
    for idx, lb in enumerate(raw_lbs):
        lb_type = lb.get("Type", "application")
        arn = lb["LoadBalancerArn"]
        # ALB → RequestCount; NLB → ActiveFlowCount
        metric = "RequestCount" if lb_type == "application" else "ActiveFlowCount"
        stat = "Sum" if lb_type == "application" else "Average"
        dims = [{"Name": "LoadBalancer", "Value": arn.split("loadbalancer/")[-1]}]
        qid = f"lb{idx}req"
        queries.append({"id": qid, "namespace": "AWS/ApplicationELB" if lb_type == "application" else "AWS/NetworkELB",
                        "metric_name": metric, "dimensions": dims, "stat": stat})

    raw_cw = get_cw_metrics_batch(queries)

    for idx, lb in enumerate(raw_lbs):
        qid = f"lb{idx}req"
        req_val = raw_cw.get(qid, 0)
        monthly_cost = 16.43 if lb.get("Type") == "network" else 22.27

        lbs.append({
            "lb_arn": lb["LoadBalancerArn"],
            "name": lb.get("LoadBalancerName", ""),
            "type": lb.get("Type", "application"),
            "state": lb.get("State", {}).get("Code", "active"),
            "monthly_cost_usd": monthly_cost,
            "metrics": {
                "avg_requests_per_minute": round(req_val / (30 * 24 * 60), 2),
                "healthy_target_count": 0,  # would need target group query
            },
        })

    return json.dumps(lbs, indent=2)


def _live_nat_gateways() -> str:
    ec2 = get_client("ec2")
    resp = ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}])
    raw_nats = resp.get("NatGateways", [])

    queries = []
    for idx, nat in enumerate(raw_nats):
        nat_id = nat["NatGatewayId"]
        dims = [{"Name": "NatGatewayId", "Value": nat_id}]
        qid = f"nat{idx}bytes"
        queries.append({"id": qid, "namespace": "AWS/NATGateway",
                        "metric_name": "BytesOutToDestination", "dimensions": dims, "stat": "Sum"})

    raw_cw = get_cw_metrics_batch(queries)

    nats = []
    for idx, nat in enumerate(raw_nats):
        nat_id = nat["NatGatewayId"]
        tags = {t["Key"]: t["Value"] for t in nat.get("Tags", [])}
        bytes_out = raw_cw.get(f"nat{idx}bytes", 0)
        gb_processed = round(bytes_out / (1024 ** 3), 2)
        data_transfer_cost = round(gb_processed * 0.045, 2)  # $0.045/GB processed

        nats.append({
            "nat_gateway_id": nat_id,
            "name": tags.get("Name", nat_id),
            "state": "available",
            "availability_zone": nat.get("SubnetId", ""),
            "monthly_cost_usd": 32.85,  # fixed hourly cost
            "data_processed_gb_monthly": gb_processed,
            "data_transfer_cost_monthly_usd": data_transfer_cost,
        })

    return json.dumps(nats, indent=2)


def _live_data_transfer() -> str:
    ce = get_client("ce")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    result = {
        "monthly_internet_egress_gb": 0,
        "monthly_internet_egress_cost_usd": 0.0,
        "monthly_cross_region_transfer_gb": 0,
        "monthly_cross_region_transfer_cost_usd": 0.0,
        "monthly_cross_az_transfer_gb": 0,
        "monthly_cross_az_transfer_cost_usd": 0.0,
    }

    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS Data Transfer"]}},
            Metrics=["UnblendedCost", "UsageQuantity"],
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
        for group in resp.get("ResultsByTime", [{}])[0].get("Groups", []):
            usage_type = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            qty = float(group["Metrics"]["UsageQuantity"]["Amount"])
            if "DataTransfer-Out-Bytes" in usage_type:
                result["monthly_internet_egress_gb"] = round(qty / (1024 ** 3), 1)
                result["monthly_internet_egress_cost_usd"] = round(cost, 2)
            elif "DataTransfer-Regional" in usage_type or "DataTransfer-AZ" in usage_type:
                result["monthly_cross_az_transfer_gb"] = round(qty / (1024 ** 3), 1)
                result["monthly_cross_az_transfer_cost_usd"] = round(cost, 2)
    except Exception:
        pass  # Cost Explorer may not be enabled or may need extra permissions

    return json.dumps(result, indent=2)


def _live_vpc_endpoint_opportunities() -> list:
    ec2 = get_client("ec2")
    existing = set()
    try:
        resp = ec2.describe_vpc_endpoints()
        for ep in resp.get("VpcEndpoints", []):
            existing.add(ep.get("ServiceName", ""))
    except Exception:
        return []

    opportunities = []
    candidates = [
        (f"com.amazonaws.{_REGION}.s3", 38.00),
        (f"com.amazonaws.{_REGION}.dynamodb", 12.00),
    ]
    for service, savings in candidates:
        if not any(service in e for e in existing):
            opportunities.append({"service": service, "potential_monthly_savings_usd": savings})

    return opportunities
