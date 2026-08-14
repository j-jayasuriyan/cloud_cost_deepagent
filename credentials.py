import threading
from dataclasses import dataclass

import boto3
import botocore.exceptions
from botocore.config import Config

_VALIDATE_CONFIG = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1})

# Memory only, by design. Nothing here is written to disk, logged, or included in
# agent context — a restart clears every entry and users re-enter their keys.
_store: dict[str, "AccountCredentials"] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class AccountCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None
    region: str
    account_id: str
    arn: str

    def describe(self) -> dict:
        """Safe to send to the browser — identity only, never key material."""
        return {
            "account_id": self.account_id,
            "arn": self.arn,
            "region": self.region,
            "access_key_hint": f"…{self.access_key_id[-4:]}",
        }


class CredentialError(Exception):
    """Supplied keys could not be used to identify an AWS account."""


def validate(access_key_id: str, secret_access_key: str,
             session_token: str | None, region: str) -> AccountCredentials:
    """
    Resolve the account these keys belong to before accepting them.

    Storing unverified keys would mean the first sign of a typo is a failed
    analysis several minutes later.
    """
    client = boto3.client(
        "sts",
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token or None,
        region_name=region,
        config=_VALIDATE_CONFIG,
    )
    try:
        identity = client.get_caller_identity()
    except botocore.exceptions.ClientError as e:
        raise CredentialError(_explain(e.response["Error"]["Code"], access_key_id)) from e
    except botocore.exceptions.BotoCoreError as e:
        raise CredentialError(f"Could not reach AWS STS in {region}.") from e

    return AccountCredentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token or None,
        region=region,
        account_id=identity["Account"],
        arn=identity["Arn"],
    )


def save(session: str, creds: AccountCredentials) -> None:
    with _lock:
        _store[session] = creds


def get(session: str | None) -> AccountCredentials | None:
    if not session:
        return None
    return _store.get(session)


def clear(session: str | None) -> None:
    if not session:
        return
    with _lock:
        _store.pop(session, None)


def _explain(code: str, access_key_id: str) -> str:
    if code in ("InvalidClientTokenId", "UnrecognizedClientException"):
        if access_key_id.startswith("ASIA"):
            return (
                "These are temporary credentials (key starts with ASIA) and need "
                "their session token as well. Paste it into the session token field."
            )
        return "That access key was not recognised by AWS."
    if code == "ExpiredToken":
        return "These credentials have expired. Generate a fresh set and try again."
    if code == "SignatureDoesNotMatch":
        return "The secret access key does not match that access key ID."
    if code == "AccessDenied":
        return "These keys are valid but lack sts:GetCallerIdentity permission."
    return f"AWS rejected these credentials ({code})."
