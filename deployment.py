import os
import threading
import time

import boto3
import botocore.exceptions
from botocore.config import Config

_PROBE_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})

# Re-probed at most this often. Long enough to keep page loads cheap, short
# enough that an expiring token is noticed while someone is still working.
_CACHE_SECONDS = 60

_lock = threading.Lock()
_checked_at = 0.0
_result: tuple[bool, str] = (True, "")
_account_id: str = ""


def account_id() -> str:
    """Account the app itself runs in — the one billed for Bedrock."""
    return _account_id


def check(force: bool = False) -> tuple[bool, str]:
    """
    Validate the deployment's own AWS credentials — the ones Bedrock runs on.

    These are separate from the keys a user connects for analysis. When they
    expire the whole app is unusable no matter what the user does, so this is a
    deployment fault rather than a user error.
    """
    global _checked_at, _result
    with _lock:
        if not force and (time.time() - _checked_at) < _CACHE_SECONDS:
            return _result
        _result = _probe_identity()
        _checked_at = time.time()
        return _result


def check_bedrock_invoke() -> tuple[bool, str]:
    """
    Confirm the model itself is reachable, not just that the token parses.

    Costs a few tokens, so this runs once at startup rather than per request.
    """
    ok, message = _probe_identity()
    if not ok:
        return ok, message

    from agents.chat_agent import _MODEL_ID

    try:
        boto3.client(
            "bedrock-runtime",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            config=_PROBE_CONFIG,
        ).converse(
            modelId=_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "ok"}]}],
            inferenceConfig={"maxTokens": 1},
        )
        return True, ""
    except botocore.exceptions.ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "AccessDeniedException":
            return False, (
                f"The deployment role cannot invoke {_MODEL_ID}. "
                "Attach bedrock:InvokeModel and bedrock:InvokeModelWithResponseStream."
            )
        return False, f"Bedrock rejected the deployment credentials ({code})."
    except botocore.exceptions.BotoCoreError as e:
        return False, f"Could not reach Bedrock: {type(e).__name__}"


def reset() -> None:
    """Clear the cached verdict. Used by tests."""
    global _checked_at, _result, _account_id
    with _lock:
        _checked_at = 0.0
        _result = (True, "")
        _account_id = ""


def _probe_identity() -> tuple[bool, str]:
    global _account_id
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    try:
        identity = boto3.client(
            "sts", region_name=region, config=_PROBE_CONFIG
        ).get_caller_identity()
        _account_id = identity["Account"]
        return True, ""
    except botocore.exceptions.ClientError as e:
        _account_id = ""
        code = e.response["Error"]["Code"]
        if code in ("ExpiredToken", "ExpiredTokenException"):
            return False, "The deployment's AWS credentials have expired."
        if code in ("InvalidClientTokenId", "UnrecognizedClientException"):
            return False, "The deployment's AWS credentials are not valid."
        return False, f"AWS rejected the deployment credentials ({code})."
    except botocore.exceptions.NoCredentialsError:
        _account_id = ""
        return False, "The deployment has no AWS credentials configured."
    except botocore.exceptions.BotoCoreError as e:
        _account_id = ""
        return False, f"Could not reach AWS STS: {type(e).__name__}"
