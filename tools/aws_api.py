import json
import datetime
import decimal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from aws_session import target_client, target_region

# Deny-by-default. Read-only verbs are the only ones this advisor needs, and the
# agent chooses operation names from model output — an allowlist keeps a bad
# generation or a prompt injection from reaching a mutating call. IAM on the
# deployed role is the real backstop; this is defense in depth.
_ALLOWED_PREFIXES = (
    "describe_",
    "list_",
    "get_",
    "lookup_",
    "head_",
    "batch_get_",
)

# Read verbs on these services return credential material or key policy.
_DENIED_SERVICES = frozenset({
    "acm-pca",
    "cognito-identity",
    "cognito-idp",
    "kms",
    "secretsmanager",
    "sso",
    "sso-admin",
    "ssm",
})

# Read-shaped operations that hand back usable credentials.
_DENIED_OPERATIONS = frozenset({
    "get_authorization_token",
    "get_credential_report",
    "get_federation_token",
    "get_parameter",
    "get_parameters",
    "get_parameters_by_path",
    "get_password_data",
    "get_session_token",
})


def _rejection(reason: str, service: str, operation: str) -> str:
    return json.dumps({
        "error": reason,
        "service": service,
        "operation": operation,
        "hint": (
            "This tool is restricted to read-only operations "
            f"({', '.join(p + '*' for p in _ALLOWED_PREFIXES)}). "
            "Report the finding and the recommended action instead of performing it."
        ),
    })


class _Encoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)


@tool
def call_aws_api(service: str, operation: str, params: str = "{}",
                 config: RunnableConfig = None) -> str:
    """
    Call any boto3 operation on any AWS service and return the result as JSON.

    Args:
        service:   boto3 service name — e.g. "ec2", "s3", "rds", "ce", "iam",
                   "cloudwatch", "lambda", "elbv2", "cloudtrail", "budgets", etc.
        operation: snake_case method name — e.g. "describe_instances",
                   "get_cost_and_usage", "list_buckets", "list_users"
        params:    JSON string of keyword arguments. Use "{}" when not needed.

    Only read-only operations are permitted — describe_*, list_*, get_*, lookup_*,
    head_*, batch_get_*. Mutating calls are rejected before reaching AWS.

    Returns:
        JSON string of the API response (ResponseMetadata stripped).
    """
    # Injected by LangChain, never exposed to the model — identifies whose
    # UI-entered credentials to read the account with.
    session = ((config or {}).get("configurable") or {}).get("session_id")

    service_key = service.strip().lower()
    operation_key = operation.strip().lower()

    if service_key in _DENIED_SERVICES:
        return _rejection(
            f"Service '{service}' is not accessible from this tool.", service, operation
        )
    if operation_key in _DENIED_OPERATIONS:
        return _rejection(
            f"Operation '{operation}' returns credential material and is blocked.",
            service, operation,
        )
    if not operation_key.startswith(_ALLOWED_PREFIXES):
        return _rejection(
            f"Operation '{operation}' is not read-only and is blocked.", service, operation
        )

    try:
        kwargs = json.loads(params) if params.strip() else {}
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in params: {e}"})

    try:
        response = getattr(target_client(service, session), operation)(**kwargs)
        response.pop("ResponseMetadata", None)
        return json.dumps(response, indent=2, cls=_Encoder)
    except Exception as e:
        return json.dumps({
            "error": str(e),
            "service": service,
            "operation": operation,
            "region": target_region(session),
        })
