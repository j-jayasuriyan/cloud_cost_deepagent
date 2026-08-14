import json

import pytest

from tools import aws_api
from tools.aws_api import call_aws_api


def run(service, operation, params="{}"):
    return call_aws_api.invoke(
        {"service": service, "operation": operation, "params": params}
    )


@pytest.fixture
def stub_boto(monkeypatch):
    """Record what reaches the AWS client without making a network call."""
    calls = []

    class _Client:
        def __getattr__(self, operation):
            def _invoke(**kwargs):
                calls.append((operation, kwargs))
                return {"Reservations": [], "ResponseMetadata": {"HTTPStatusCode": 200}}
            return _invoke

    monkeypatch.setattr(aws_api, "target_client", lambda service, session=None: _Client())
    return calls


@pytest.fixture
def seen_sessions(monkeypatch):
    """Capture the login session each AWS client is built for."""
    sessions = []

    class _Client:
        def __getattr__(self, operation):
            return lambda **kwargs: {"Reservations": []}

    def _target_client(service, session=None):
        sessions.append(session)
        return _Client()

    monkeypatch.setattr(aws_api, "target_client", _target_client)
    return sessions


def should_allow_when_operation_is_read_only(stub_boto):
    result = json.loads(run("ec2", "describe_instances", "{}"))

    assert "error" not in result
    assert stub_boto == [("describe_instances", {})]


def should_strip_response_metadata_when_call_succeeds(stub_boto):
    result = json.loads(run("ec2", "describe_instances", "{}"))

    assert "ResponseMetadata" not in result


def should_reject_when_operation_is_mutating(stub_boto):
    result = json.loads(run("ec2", "terminate_instances", '{"InstanceIds": ["i-1"]}'))

    assert "not read-only" in result["error"]
    assert stub_boto == []


@pytest.mark.parametrize(
    "operation",
    ["delete_volume", "run_instances", "put_object", "modify_db_instance", "create_user"],
)
def should_reject_when_operation_has_any_mutating_verb(stub_boto, operation):
    result = json.loads(run("ec2", operation, "{}"))

    assert "error" in result
    assert stub_boto == []


@pytest.mark.parametrize("service", ["secretsmanager", "kms", "ssm", "cognito-idp"])
def should_reject_when_service_holds_credentials(stub_boto, service):
    result = json.loads(run(service, "list_secrets", "{}"))

    assert "not accessible" in result["error"]
    assert stub_boto == []


@pytest.mark.parametrize(
    "operation",
    ["get_session_token", "get_federation_token", "get_password_data", "get_credential_report"],
)
def should_reject_when_read_operation_returns_credentials(stub_boto, operation):
    result = json.loads(run("sts", operation, "{}"))

    assert "credential material" in result["error"]
    assert stub_boto == []


def should_reject_when_casing_disguises_a_mutating_call(stub_boto):
    result = json.loads(run("EC2", "TerminateInstances", "{}"))

    assert "error" in result
    assert stub_boto == []


def should_explain_the_restriction_when_rejecting(stub_boto):
    result = json.loads(run("ec2", "terminate_instances", "{}"))

    assert "read-only" in result["hint"]
    assert result["service"] == "ec2"
    assert result["operation"] == "terminate_instances"


def should_report_error_when_params_are_not_valid_json(stub_boto):
    result = json.loads(run("ec2", "describe_instances", "{not json"))

    assert "Invalid JSON" in result["error"]
    assert stub_boto == []


def should_use_callers_session_when_config_carries_one(seen_sessions):
    call_aws_api.invoke(
        {"service": "ec2", "operation": "describe_instances", "params": "{}"},
        {"configurable": {"session_id": "login-abc", "thread_id": "t1"}},
    )

    assert seen_sessions == ["login-abc"]


def should_fall_back_to_environment_when_config_has_no_session(seen_sessions):
    run("ec2", "describe_instances", "{}")

    assert seen_sessions == [None]


def should_hide_config_from_the_model_schema():
    assert "config" not in call_aws_api.args
