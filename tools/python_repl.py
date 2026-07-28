import json
import subprocess
import sys
import textwrap
from langchain_core.tools import tool

_TIMEOUT_SECONDS = 10
_MAX_OUTPUT_CHARS = 8000


@tool
def execute_python(code: str, context_json: str = "") -> str:
    """
    Execute Python code in a sandboxed subprocess with a 10-second timeout.

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
    wrapper = textwrap.dedent("""\
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

    try:
        result = subprocess.run(
            [sys.executable, "-c", full_code],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
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
