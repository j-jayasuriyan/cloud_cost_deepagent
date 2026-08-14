import os
import subprocess
import sys
import tempfile
import textwrap
from langchain_core.tools import tool

_TIMEOUT_SECONDS = 10
_MAX_OUTPUT_CHARS = 8000

_CPU_SECONDS = 10
_MAX_FILE_BYTES = 10 * 1024 * 1024

# Only these are forwarded to the child. Everything else — notably AWS_* and
# LANGCHAIN_* — is withheld so generated code cannot authenticate as this process
# and route around the call_aws_api allowlist.
_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE")

# Applied inside the child before user code runs. preexec_fn is unsafe from the
# server's thread pool, and limits set here cannot be raised again by user code
# because the hard limit is lowered to match.
_LIMITS_PREAMBLE = f"""\
import resource as _resource
_resource.setrlimit(_resource.RLIMIT_CPU,   ({_CPU_SECONDS}, {_CPU_SECONDS}))
_resource.setrlimit(_resource.RLIMIT_FSIZE, ({_MAX_FILE_BYTES}, {_MAX_FILE_BYTES}))
_resource.setrlimit(_resource.RLIMIT_CORE,  (0, 0))
del _resource
"""


def _child_env(workdir: str) -> dict[str, str]:
    env = {name: os.environ[name] for name in _ENV_PASSTHROUGH if name in os.environ}
    env["HOME"] = workdir
    env["TMPDIR"] = workdir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # HOME above hides ~/.aws; this closes the other credential source botocore
    # would reach for on EC2/ECS. Blocking egress to 169.254.169.254 at the
    # container level is the real fix — this only stops boto3, not raw HTTP.
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


@tool
def execute_python(code: str, context_json: str = "") -> str:
    """
    Execute Python code in an isolated subprocess with a 10-second timeout.

    The subprocess runs with no AWS credentials and no network-service secrets, so
    it cannot call AWS. Pass any AWS data you need in through context_json.

    If context_json is provided (a JSON string from another tool), it is available
    inside the code as the variable `_ctx` — already parsed, so use it directly
    (do NOT call json.loads on it again).

    The code must print its result to stdout. Whatever is printed becomes the return value.

    Use this for: arithmetic on cost data, aggregations, trend calculations,
    impact simulations, filtering/sorting, or any transformation of raw AWS data.

    Example:
        data = call_aws_api("ec2", "describe_volumes", "{}")
        code = '''
        vols = [v for v in _ctx["Volumes"] if v["State"] == "available"]
        total = sum(v["Size"] * 0.10 for v in vols)
        print(f"Unattached volume waste: ${total:.2f}/month")
        '''
    """
    wrapper = _LIMITS_PREAMBLE + textwrap.dedent("""\
        import json as _json
        import sys as _sys

        _ctx_raw = _JSON_PLACEHOLDER_
        if _ctx_raw:
            try:
                _ctx = _json.loads(_ctx_raw)
            except Exception:
                _ctx = _ctx_raw
        else:
            _ctx = None

    """).replace("_JSON_PLACEHOLDER_", repr(context_json))

    full_code = wrapper + textwrap.dedent(code)

    with tempfile.TemporaryDirectory(prefix="pyexec-") as workdir:
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-c", full_code],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                env=_child_env(workdir),
                cwd=workdir,
            )
        except subprocess.TimeoutExpired:
            return f"Error: code execution timed out after {_TIMEOUT_SECONDS} seconds."

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        # Return stderr so the agent can self-correct its code
        return f"Error (exit {result.returncode}):\n{stderr[:2000]}"

    if not stdout and stderr:
        # Warnings printed to stderr but exit 0 — return both
        return f"{stderr[:500]}\n{stdout}"

    output = stdout or "(no output)"
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + f"\n... (truncated at {_MAX_OUTPUT_CHARS} chars)"

    return output
