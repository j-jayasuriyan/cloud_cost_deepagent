import json

import pytest

from tools import python_repl
from tools.python_repl import execute_python


def run(code: str, context_json: str = "") -> str:
    return execute_python.invoke({"code": code, "context_json": context_json})


@pytest.fixture
def parent_secrets(monkeypatch):
    """Credentials present in the server process, as load_dotenv would set them."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLENOTREAL")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexamplenotreal")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tokenexamplenotreal")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "lsexamplenotreal")


def should_hide_aws_credentials_when_parent_has_them(parent_secrets):
    output = run(
        'import os; print([k for k in os.environ if k.startswith("AWS_") '
        'and k != "AWS_EC2_METADATA_DISABLED"])'
    )

    assert output == "[]"


def should_hide_langchain_secrets_when_parent_has_them(parent_secrets):
    output = run('import os; print([k for k in os.environ if k.startswith("LANGCHAIN_")])')

    assert output == "[]"


def should_block_boto3_when_parent_is_authenticated(parent_secrets):
    output = run(
        "import boto3\n"
        "try:\n"
        "    boto3.client('sts', region_name='us-east-1').get_caller_identity()\n"
        "    print('LEAKED')\n"
        "except Exception as e:\n"
        "    print(type(e).__name__)\n"
    )

    assert output == "NoCredentialsError"


def should_not_expose_home_directory_when_executing():
    output = run('import os; print(os.path.exists(os.path.expanduser("~/.aws")))')

    assert output == "False"


def should_expose_ctx_when_context_json_is_provided():
    volumes = {"Volumes": [{"Size": 100, "State": "available"},
                           {"Size": 50, "State": "in-use"}]}

    output = run(
        'print(sum(v["Size"] for v in _ctx["Volumes"] if v["State"] == "available"))',
        json.dumps(volumes),
    )

    assert output == "100"


def should_set_ctx_to_none_when_no_context_is_provided():
    assert run("print(_ctx)") == "None"


def should_limit_cpu_time_when_executing():
    output = run("import resource; print(resource.getrlimit(resource.RLIMIT_CPU))")

    assert output == f"({python_repl._CPU_SECONDS}, {python_repl._CPU_SECONDS})"


def should_disable_core_dumps_when_executing():
    output = run("import resource; print(resource.getrlimit(resource.RLIMIT_CORE))")

    assert output == "(0, 0)"


def should_return_traceback_when_code_raises():
    output = run("print(1 / 0)")

    assert "ZeroDivisionError" in output
    assert output.startswith("Error (exit 1)")


def should_time_out_when_code_blocks(monkeypatch):
    monkeypatch.setattr(python_repl, "_TIMEOUT_SECONDS", 1)

    output = run("import time; time.sleep(30)")

    assert "timed out" in output


def should_truncate_when_output_exceeds_limit():
    output = run(f'print("x" * {python_repl._MAX_OUTPUT_CHARS + 500})')

    assert "truncated" in output
    assert len(output) < python_repl._MAX_OUTPUT_CHARS + 200


def should_report_no_output_when_nothing_is_printed():
    assert run("x = 1 + 1") == "(no output)"
