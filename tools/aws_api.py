import os
import json
import datetime
import decimal
import boto3
from botocore.config import Config

_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=60,
    retries={"max_attempts": 3, "mode": "adaptive"},
)


class _Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)


def call_aws_api(service: str, operation: str, params: str = "{}") -> str:
    """
    Call any boto3 operation on any AWS service and return the result as JSON.

    Args:
        service:   boto3 service name — e.g. "ec2", "s3", "rds", "ce", "iam",
                   "cloudwatch", "lambda", "elbv2", "cloudtrail", "budgets", etc.
        operation: snake_case method name — e.g. "describe_instances",
                   "get_cost_and_usage", "list_buckets", "list_users"
        params:    JSON string of keyword arguments. Use "{}" when not needed.

    Returns:
        JSON string of the API response (ResponseMetadata stripped).
    """
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    try:
        kwargs = json.loads(params) if params.strip() else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in params: {e}"})

    try:
        client = boto3.client(service, region_name=region, config=_BOTO_CONFIG)
        response = getattr(client, operation)(**kwargs)
        response.pop("ResponseMetadata", None)
        return json.dumps(response, indent=2, cls=_Encoder)
    except Exception as e:
        return json.dumps({"error": str(e), "service": service, "operation": operation})
