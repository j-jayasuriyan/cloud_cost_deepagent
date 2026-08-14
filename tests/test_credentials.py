import pytest

import credentials
from credentials import AccountCredentials


def make(access_key_id="AKIAEXAMPLENOTREAL12", account_id="111122223333"):
    return AccountCredentials(
        access_key_id=access_key_id,
        secret_access_key="secretnotreal",
        session_token=None,
        region="us-east-1",
        account_id=account_id,
        arn=f"arn:aws:iam::{account_id}:user/reader",
    )


@pytest.fixture(autouse=True)
def clean_store():
    credentials._store.clear()
    yield
    credentials._store.clear()


def should_return_none_when_session_has_no_credentials():
    assert credentials.get("session-1") is None


def should_return_none_when_session_is_missing():
    assert credentials.get(None) is None


def should_return_credentials_when_saved_for_that_session():
    credentials.save("session-1", make())

    assert credentials.get("session-1").account_id == "111122223333"


def should_isolate_credentials_when_sessions_differ():
    credentials.save("session-1", make(account_id="111122223333"))
    credentials.save("session-2", make(account_id="444455556666"))

    assert credentials.get("session-1").account_id == "111122223333"
    assert credentials.get("session-2").account_id == "444455556666"


def should_forget_credentials_when_cleared():
    credentials.save("session-1", make())
    credentials.clear("session-1")

    assert credentials.get("session-1") is None


def should_not_raise_when_clearing_an_unknown_session():
    credentials.clear("never-existed")
    credentials.clear(None)


def should_omit_key_material_when_describing():
    described = make().describe()

    assert "secretnotreal" not in str(described)
    assert "AKIAEXAMPLENOTREAL12" not in str(described)
    assert described["access_key_hint"] == "…AL12"
    assert described["account_id"] == "111122223333"


def should_explain_missing_session_token_when_key_is_temporary():
    message = credentials._explain("InvalidClientTokenId", "ASIAEXAMPLENOTREAL12")

    assert "session token" in message


def should_not_mention_session_token_when_key_is_long_lived():
    message = credentials._explain("InvalidClientTokenId", "AKIAEXAMPLENOTREAL12")

    assert "session token" not in message


@pytest.mark.parametrize(
    "code,expected",
    [
        ("ExpiredToken", "expired"),
        ("SignatureDoesNotMatch", "does not match"),
        ("AccessDenied", "GetCallerIdentity"),
    ],
)
def should_explain_the_failure_when_aws_rejects_the_keys(code, expected):
    assert expected in credentials._explain(code, "AKIAEXAMPLENOTREAL12")


def should_never_echo_the_key_when_explaining():
    message = credentials._explain("SignatureDoesNotMatch", "AKIASECRETLOOKING123")

    assert "AKIASECRETLOOKING123" not in message
