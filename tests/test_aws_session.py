import pytest

import aws_session
import credentials
from credentials import AccountCredentials


def make(access_key_id="AKIAEXAMPLENOTREAL12", region="us-east-1", account_id="111122223333"):
    return AccountCredentials(
        access_key_id=access_key_id,
        secret_access_key="secretnotreal",
        session_token=None,
        region=region,
        account_id=account_id,
        arn=f"arn:aws:iam::{account_id}:user/reader",
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    # Deployment credentials must never leak into the analysis path.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "TARGET_AWS_ACCESS_KEY_ID", "TARGET_AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    credentials._store.clear()
    aws_session.reset()
    yield
    credentials._store.clear()
    aws_session.reset()


def should_report_no_credentials_when_session_has_none():
    assert not aws_session.has_credentials("session-1")


def should_report_credentials_when_session_has_them():
    credentials.save("session-1", make())

    assert aws_session.has_credentials("session-1")


def should_refuse_to_build_a_client_when_session_has_no_credentials():
    with pytest.raises(aws_session.MissingCredentials):
        aws_session.target_client("ec2", "session-1")


def should_refuse_to_build_a_client_when_there_is_no_session():
    with pytest.raises(aws_session.MissingCredentials):
        aws_session.target_client("ec2", None)


def should_ignore_environment_keys_when_session_has_none(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIADEPLOYMENTNOTREAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "deploymentsecretnotreal")
    monkeypatch.setenv("TARGET_AWS_ACCESS_KEY_ID", "AKIATARGETNOTREAL123")
    monkeypatch.setenv("TARGET_AWS_SECRET_ACCESS_KEY", "targetsecretnotreal")

    with pytest.raises(aws_session.MissingCredentials):
        aws_session.target_client("ec2", "session-1")


def should_use_session_credentials_when_present():
    credentials.save("session-1", make())

    client = aws_session.target_client("ec2", "session-1")

    assert client.meta.region_name == "us-east-1"


def should_isolate_clients_when_sessions_differ():
    credentials.save("session-1", make(region="us-east-1"))
    credentials.save("session-2", make(region="eu-west-2"))

    assert aws_session.target_client("ec2", "session-1").meta.region_name == "us-east-1"
    assert aws_session.target_client("ec2", "session-2").meta.region_name == "eu-west-2"


def should_reuse_client_when_requested_twice():
    credentials.save("session-1", make())

    assert aws_session.target_client("ec2", "session-1") is \
           aws_session.target_client("ec2", "session-1")


def should_build_separate_clients_when_services_differ():
    credentials.save("session-1", make())

    assert aws_session.target_client("ec2", "session-1") is not \
           aws_session.target_client("s3", "session-1")


def should_drop_cached_clients_when_session_is_forgotten():
    credentials.save("session-1", make())
    first = aws_session.target_client("ec2", "session-1")

    aws_session.forget_session("session-1")

    assert aws_session.target_client("ec2", "session-1") is not first


def should_keep_other_sessions_when_one_is_forgotten():
    credentials.save("session-1", make())
    credentials.save("session-2", make())
    kept = aws_session.target_client("ec2", "session-2")

    aws_session.forget_session("session-1")

    assert aws_session.target_client("ec2", "session-2") is kept


def should_use_session_region_when_reporting_region():
    credentials.save("session-1", make(region="ap-south-1"))

    assert aws_session.target_region("session-1") == "ap-south-1"


def should_redact_the_key_when_describing_the_source():
    credentials.save("session-1", make(access_key_id="AKIASECRETLOOKING123"))

    described = aws_session._describe_source("session-1")

    assert "AKIASECRETLOOKING123" not in described
    assert described.endswith("G123")


def should_say_so_when_no_credentials_to_describe():
    assert aws_session._describe_source("session-1") == "no credentials"
