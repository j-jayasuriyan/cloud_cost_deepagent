import threading

import boto3
import botocore.exceptions
from botocore.config import Config

import credentials

_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=60,
    retries={"max_attempts": 3, "mode": "adaptive"},
)

_DEFAULT_REGION = "us-east-1"

# Keyed by login session and service. Never keyed on key material.
_clients: dict[tuple[str, str, str], object] = {}
_lock = threading.Lock()


class MissingCredentials(Exception):
    """No AWS keys have been entered for this login session."""


def has_credentials(session: str | None) -> bool:
    return credentials.get(session) is not None


def target_region(session: str | None) -> str:
    creds = credentials.get(session)
    return creds.region if creds else _DEFAULT_REGION


def target_client(service: str, session: str | None = None):
    """
    Client for the account being analysed.

    Credentials come only from what the signed-in user entered. There is no
    environment or instance-role fallback — the app must never read an account
    the user did not explicitly connect.
    """
    creds = credentials.get(session)
    if creds is None:
        raise MissingCredentials(
            "No AWS credentials for this session. Enter an access key, secret key, "
            "and (for temporary ASIA keys) a session token in the AWS Account panel."
        )

    key = (session, service, creds.region)
    client = _clients.get(key)
    if client is not None:
        return client

    with _lock:
        if key not in _clients:
            _clients[key] = boto3.Session(
                aws_access_key_id=creds.access_key_id,
                aws_secret_access_key=creds.secret_access_key,
                aws_session_token=creds.session_token,
                region_name=creds.region,
            ).client(service, region_name=creds.region, config=_BOTO_CONFIG)
        return _clients[key]


def verify_target_identity(session: str | None) -> dict:
    """Resolve the account this session will read. Raises rather than guessing."""
    try:
        return target_client("sts", session).get_caller_identity()
    except botocore.exceptions.ClientError as e:
        raise RuntimeError(
            f"Could not verify the connected AWS account "
            f"({_describe_source(session)}, region {target_region(session)}): "
            f"{e.response['Error']['Code']}"
        ) from e


def forget_session(session: str | None) -> None:
    """Drop cached clients for one login session. Call whenever its keys change."""
    if not session:
        return
    with _lock:
        for key in [k for k in _clients if k[0] == session]:
            del _clients[key]


def reset() -> None:
    """Drop every cached client. Used by tests."""
    with _lock:
        _clients.clear()


def _describe_source(session: str | None) -> str:
    """Identify the credentials without echoing them."""
    creds = credentials.get(session)
    if creds is None:
        return "no credentials"
    return f"key ending {creds.access_key_id[-4:]}"
