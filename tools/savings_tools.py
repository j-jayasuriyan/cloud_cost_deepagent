import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from tools.aws_client import USE_LIVE, get_client, _REGION

_MOCK = json.loads((Path(__file__).parent.parent / "data" / "mock_savings.json").read_text())


# ── public tools ───────────────────────────────────────────────────────────────

def get_current_spend_summary() -> str:
    """Return high-level monthly spend breakdown and what fraction is on-demand (uncovered)."""
    return _live_spend_summary() if USE_LIVE else json.dumps(_MOCK["current_spend_summary"], indent=2)


def get_active_savings_plans() -> str:
    """Return active Savings Plans with utilization rate and monthly savings vs on-demand."""
    return _live_savings_plans() if USE_LIVE else json.dumps(_MOCK["savings_plans"], indent=2)


def get_active_reserved_instances() -> str:
    """Return active Reserved Instances across EC2 and RDS."""
    return _live_reserved_instances() if USE_LIVE else json.dumps(_MOCK["reserved_instances"], indent=2)


def get_coverage_analysis() -> str:
    """
    Return RI/SP coverage gaps and pre-computed recommendations:
    EC2/RDS on-demand coverage %, recommended Compute SP commitment, per-instance RI opportunities.
    """
    return _live_coverage_analysis() if USE_LIVE else json.dumps(_MOCK["coverage_analysis"], indent=2)


def analyze_savings_plan_recommendations() -> str:
    """
    Synthesise the full RI/SP opportunity: current gaps, recommended Compute SP,
    per-instance RI recommendations, and total monthly savings potential.
    """
    ca = json.loads(_live_coverage_analysis() if USE_LIVE else json.dumps(_MOCK["coverage_analysis"]))
    sp_savings = ca.get("recommended_compute_sp_estimated_monthly_savings_usd", 0)
    ec2_ri_savings = sum(r["monthly_savings_usd"] for r in ca.get("recommended_ec2_ri_opportunities", []))
    rds_ri_savings = sum(r["monthly_savings_usd"] for r in ca.get("recommended_rds_ri_opportunities", []))

    return json.dumps({
        "coverage_summary": {
            "ec2_on_demand_coverage_percent": ca.get("ec2_on_demand_coverage_percent", 0),
            "rds_on_demand_coverage_percent": ca.get("rds_on_demand_coverage_percent", 0),
            "uncovered_monthly_spend_usd": ca.get("total_uncovered_monthly_spend_usd", 0),
        },
        "recommended_actions": [
            {
                "action": "Purchase Compute Savings Plan",
                "commitment_per_hour_usd": ca.get("recommended_compute_sp_hourly_commitment", 0),
                "term": "1 year, No Upfront",
                "estimated_monthly_savings_usd": sp_savings,
                "risk": "LOW",
            },
            {
                "action": "Purchase RDS Reserved Instances",
                "opportunities": ca.get("recommended_rds_ri_opportunities", []),
                "total_monthly_savings_usd": rds_ri_savings,
                "risk": "LOW",
            },
        ],
        "total_conservative_monthly_savings_usd": round(sp_savings + rds_ri_savings, 2),
        "total_aggressive_monthly_savings_usd": round(sp_savings + ec2_ri_savings + rds_ri_savings, 2),
    }, indent=2)


# ── live implementations ───────────────────────────────────────────────────────

def _ce_date_range(days: int = 30):
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return start, end


def _live_spend_summary() -> str:
    ce = get_client("ce")
    start, end = _ce_date_range(30)
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "PURCHASE_TYPE"}],
        )
        totals = {"monthly_on_demand_compute_usd": 0.0, "monthly_total_usd": 0.0}
        for group in resp.get("ResultsByTime", [{}])[0].get("Groups", []):
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            totals["monthly_total_usd"] += cost
            if group["Keys"][0] == "On Demand":
                totals["monthly_on_demand_compute_usd"] += cost
        totals = {k: round(v, 2) for k, v in totals.items()}
        return json.dumps(totals, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e), "note": "Cost Explorer may not be enabled"}, indent=2)


def _live_savings_plans() -> str:
    ce = get_client("ce")
    start, end = _ce_date_range(30)
    plans = []
    try:
        resp = ce.describe_savings_plans(states=["active"])
        for sp in resp.get("savingsPlans", []):
            commitment = float(sp.get("commitment", 0))
            plans.append({
                "savings_plan_id": sp.get("savingsPlanId", ""),
                "type": sp.get("savingsPlanType", ""),
                "commitment_usd_per_hour": commitment,
                "term_years": round(int(sp.get("termDurationInSeconds", 31536000)) / 31536000),
                "payment_option": sp.get("paymentOption", ""),
                "state": sp.get("state", ""),
                "monthly_commitment_usd": round(commitment * 730, 2),
                "utilization_percent": 0.0,
                "monthly_savings_vs_ondemand_usd": 0.0,
            })
    except Exception:
        pass
    return json.dumps(plans, indent=2)


def _live_reserved_instances() -> str:
    ec2 = get_client("ec2")
    rds = get_client("rds")
    ris = []

    try:
        resp = ec2.describe_reserved_instances(Filters=[{"Name": "state", "Values": ["active"]}])
        for ri in resp.get("ReservedInstances", []):
            ris.append({
                "reserved_instance_id": ri.get("ReservedInstancesId", ""),
                "service": "EC2",
                "instance_type": ri.get("InstanceType", ""),
                "region": (az[:-1] if (az := ri.get("AvailabilityZone", "")) else _REGION),
                "term_years": round(ri.get("Duration", 31536000) / 31536000),
                "payment_option": ri.get("OfferingType", ""),
                "state": ri.get("State", ""),
                "monthly_savings_vs_ondemand_usd": 0.0,
            })
    except Exception:
        pass

    try:
        paginator = rds.get_paginator("describe_reserved_db_instances")
        for page in paginator.paginate():
            for ri in page["ReservedDBInstances"]:
                if ri.get("State") != "active":
                    continue
                ris.append({
                    "reserved_instance_id": ri.get("ReservedDBInstanceId", ""),
                    "service": "RDS",
                    "instance_type": ri.get("DBInstanceClass", ""),
                    "region": _REGION,
                    "term_years": round(ri.get("Duration", 31536000) / 31536000),
                    "payment_option": ri.get("OfferingType", ""),
                    "state": ri.get("State", ""),
                    "monthly_savings_vs_ondemand_usd": 0.0,
                })
    except Exception:
        pass

    return json.dumps(ris, indent=2)


def _live_coverage_analysis() -> str:
    ce = get_client("ce")
    start, end = _ce_date_range(30)

    result = {
        "ec2_on_demand_coverage_percent": 0.0,
        "rds_on_demand_coverage_percent": 0.0,
        "total_uncovered_monthly_spend_usd": 0.0,
        "recommended_compute_sp_hourly_commitment": 0.0,
        "recommended_compute_sp_estimated_monthly_savings_usd": 0.0,
        "recommended_ec2_ri_opportunities": [],
        "recommended_rds_ri_opportunities": [],
    }

    try:
        # SP coverage for EC2
        sp_cov = ce.get_savings_plans_coverage(
            TimePeriod={"Start": start, "End": end},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        for grp in sp_cov.get("SavingsPlansCoverages", []):
            for item in grp.get("SavingsPlansCoverages", []):
                svc = item.get("Attributes", {}).get("SERVICE", "")
                if "EC2" in svc:
                    result["ec2_on_demand_coverage_percent"] = round(
                        float(item["Coverage"].get("CoveragePercentage", 0)), 1)
    except Exception:
        pass

    try:
        # SP purchase recommendation
        sp_rec = ce.get_savings_plans_purchase_recommendation(
            SavingsPlansType="COMPUTE_SP",
            TermInYears="ONE_YEAR",
            PaymentOption="NO_UPFRONT",
            LookbackPeriodInDays="THIRTY_DAYS",
        )
        summary = sp_rec.get("SavingsPlansPurchaseRecommendationSummary", {})
        result["recommended_compute_sp_hourly_commitment"] = round(
            float(summary.get("HourlyCommitmentToPurchase", 0)), 2)
        result["recommended_compute_sp_estimated_monthly_savings_usd"] = round(
            float(summary.get("EstimatedMonthlySavingsAmount", 0)), 2)
        result["total_uncovered_monthly_spend_usd"] = round(
            float(summary.get("TotalRecommendationCount", 0)) * 100, 2)
    except Exception:
        pass

    try:
        # RDS RI recommendations
        rds_rec = ce.get_reservation_purchase_recommendation(
            Service="Amazon RDS",
            TermInYears="ONE_YEAR",
            PaymentOption="PARTIAL_UPFRONT",
            LookbackPeriodInDays="THIRTY_DAYS",
        )
        for rec in rds_rec.get("Recommendations", []):
            for detail in rec.get("RecommendationDetails", [])[:5]:
                spec = detail.get("InstanceDetails", {}).get("RDSInstanceDetails", {})
                result["recommended_rds_ri_opportunities"].append({
                    "db_instance_class": spec.get("InstanceType", ""),
                    "engine": spec.get("DatabaseEngine", ""),
                    "multi_az": spec.get("MultiAZ", False),
                    "term": "1yr",
                    "payment": "Partial Upfront",
                    "monthly_savings_usd": round(
                        float(detail.get("EstimatedMonthlySavingsAmount", 0)), 2),
                })
    except Exception:
        pass

    return json.dumps(result, indent=2)
