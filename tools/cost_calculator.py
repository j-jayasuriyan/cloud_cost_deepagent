"""
AWS pricing constants and savings estimation helpers.
Prices are approximate us-east-1 on-demand rates (2025).
"""

# EC2 on-demand monthly prices (Linux, us-east-1)
EC2_MONTHLY_PRICES = {
    "t3.nano": 3.80, "t3.micro": 7.59, "t3.small": 15.18,
    "t3.medium": 30.37, "t3.large": 60.74, "t3.xlarge": 120.48, "t3.2xlarge": 240.96,
    "m5.large": 69.12, "m5.xlarge": 138.24, "m5.2xlarge": 276.48,
    "m5.4xlarge": 552.96, "m5.8xlarge": 1105.92,
    "c5.large": 61.20, "c5.xlarge": 122.40, "c5.2xlarge": 244.80,
    "c5.4xlarge": 489.60, "c5.9xlarge": 1101.60,
    "r5.large": 90.52, "r5.xlarge": 181.04, "r5.2xlarge": 362.08,
    "r5.4xlarge": 724.16, "r5.8xlarge": 1448.32,
}

# Rightsizing map: oversized → recommended
EC2_RIGHTSIZE_MAP = {
    "m5.2xlarge": "t3.large",
    "m5.4xlarge": "m5.xlarge",
    "c5.4xlarge": "c5.xlarge",
    "c5.2xlarge": "t3.xlarge",
    "r5.4xlarge": "r5.xlarge",
    "r5.2xlarge": "r5.large",
    "t3.xlarge": "t3.small",
}

# EBS pricing per GB/month
EBS_PRICE_PER_GB = {
    "gp2": 0.10,
    "gp3": 0.08,
    "io1": 0.125,
    "io2": 0.125,
    "st1": 0.045,
    "sc1": 0.025,
}

# S3 storage class pricing per GB/month
S3_PRICE_PER_GB = {
    "STANDARD": 0.023,
    "STANDARD_IA": 0.0125,
    "INTELLIGENT_TIERING": 0.023,
    "GLACIER_IR": 0.004,
    "GLACIER": 0.0036,
    "DEEP_ARCHIVE": 0.00099,
}


def estimate_ec2_rightsize_savings(instance_type: str, monthly_cost: float) -> dict:
    recommended = EC2_RIGHTSIZE_MAP.get(instance_type)
    if not recommended:
        return {"recommended_type": None, "monthly_savings_usd": 0.0}
    new_cost = EC2_MONTHLY_PRICES.get(recommended, monthly_cost * 0.5)
    return {
        "recommended_type": recommended,
        "current_monthly_cost_usd": monthly_cost,
        "new_monthly_cost_usd": round(new_cost, 2),
        "monthly_savings_usd": round(monthly_cost - new_cost, 2),
        "savings_percent": round((monthly_cost - new_cost) / monthly_cost * 100, 1),
    }


def estimate_ebs_gp2_to_gp3_savings(size_gb: int) -> dict:
    gp2_cost = size_gb * EBS_PRICE_PER_GB["gp2"]
    gp3_cost = size_gb * EBS_PRICE_PER_GB["gp3"]
    return {
        "current_monthly_cost_usd": round(gp2_cost, 2),
        "new_monthly_cost_usd": round(gp3_cost, 2),
        "monthly_savings_usd": round(gp2_cost - gp3_cost, 2),
        "savings_percent": 20.0,
    }


def estimate_s3_lifecycle_savings(size_gb: float, cold_percent: float) -> dict:
    cold_gb = size_gb * (cold_percent / 100)
    current_cost = cold_gb * S3_PRICE_PER_GB["STANDARD"]
    new_cost = cold_gb * S3_PRICE_PER_GB["GLACIER_IR"]
    return {
        "cold_data_gb": round(cold_gb, 1),
        "current_monthly_cost_usd": round(current_cost, 2),
        "new_monthly_cost_usd": round(new_cost, 2),
        "monthly_savings_usd": round(current_cost - new_cost, 2),
    }


def estimate_lambda_rightsize_savings(
    current_mb: int, avg_used_mb: int, monthly_cost: float
) -> dict:
    optimal_mb = min(
        next((m for m in [128, 256, 512, 1024, 1536, 2048, 3008] if m >= avg_used_mb * 1.3), current_mb),
        current_mb,
    )
    if optimal_mb >= current_mb:
        return {"recommended_mb": current_mb, "monthly_savings_usd": 0.0}
    ratio = optimal_mb / current_mb
    new_cost = monthly_cost * ratio
    return {
        "recommended_mb": optimal_mb,
        "current_mb": current_mb,
        "current_monthly_cost_usd": round(monthly_cost, 2),
        "new_monthly_cost_usd": round(new_cost, 2),
        "monthly_savings_usd": round(monthly_cost - new_cost, 2),
        "savings_percent": round((1 - ratio) * 100, 1),
    }
