import botocore.exceptions
import pytest

import deployment


def client_error(code):
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "simulated"}}, "GetCallerIdentity"
    )


@pytest.fixture(autouse=True)
def clean():
    deployment.reset()
    yield
    deployment.reset()


@pytest.fixture
def sts_raises(monkeypatch):
    def _raise_with(exc):
        class _Client:
            def get_caller_identity(self):
                raise exc
        monkeypatch.setattr(deployment.boto3, "client", lambda *a, **kw: _Client())
    return _raise_with


@pytest.fixture
def sts_succeeds(monkeypatch):
    calls = []

    class _Client:
        def get_caller_identity(self):
            calls.append(1)
            return {"Account": "111122223333", "Arn": "arn:aws:iam::111122223333:user/x"}

    monkeypatch.setattr(deployment.boto3, "client", lambda *a, **kw: _Client())
    return calls


def should_report_healthy_when_credentials_work(sts_succeeds):
    assert deployment.check() == (True, "")


def should_report_unhealthy_when_token_has_expired(sts_raises):
    sts_raises(client_error("ExpiredToken"))

    ok, why = deployment.check()

    assert not ok
    assert "expired" in why


def should_report_unhealthy_when_token_is_invalid(sts_raises):
    sts_raises(client_error("InvalidClientTokenId"))

    ok, why = deployment.check()

    assert not ok
    assert "not valid" in why


def should_report_unhealthy_when_no_credentials_are_configured(sts_raises):
    sts_raises(botocore.exceptions.NoCredentialsError())

    ok, why = deployment.check()

    assert not ok
    assert "no AWS credentials" in why


def should_name_the_error_code_when_aws_rejects_for_another_reason(sts_raises):
    sts_raises(client_error("AccessDenied"))

    ok, why = deployment.check()

    assert not ok
    assert "AccessDenied" in why


def should_cache_the_verdict_when_checked_twice(sts_succeeds):
    deployment.check()
    deployment.check()

    assert len(sts_succeeds) == 1


def should_probe_again_when_forced(sts_succeeds):
    deployment.check()
    deployment.check(force=True)

    assert len(sts_succeeds) == 2


def should_probe_again_when_cache_has_expired(sts_succeeds, monkeypatch):
    deployment.check()
    monkeypatch.setattr(deployment, "_checked_at", 0.0)

    deployment.check()

    assert len(sts_succeeds) == 2


def should_not_reach_bedrock_when_identity_already_fails(sts_raises):
    sts_raises(client_error("ExpiredToken"))

    ok, why = deployment.check_bedrock_invoke()

    assert not ok
    assert "expired" in why
