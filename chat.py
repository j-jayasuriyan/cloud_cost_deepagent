import os
import sys
import time
import webbrowser
import threading
from pathlib import Path

# If packages aren't importable (running outside venv), re-exec with the venv Python.
try:
    import uvicorn  # noqa: F401
except ImportError:
    _venv_python = Path(__file__).parent / ".venv" / "bin" / "python3"
    if _venv_python.exists():
        os.execv(str(_venv_python), [str(_venv_python)] + sys.argv)
    print("Error: uvicorn not found. Run: pip install uvicorn fastapi")
    sys.exit(1)

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

HOST = "127.0.0.1"
PORT = 8000
URL  = f"http://{HOST}:{PORT}"


def _check_credentials():
    """
    Resolve credentials the way boto3 will at runtime.

    Checking AWS_ACCESS_KEY_ID directly would reject instance profiles and ECS
    task roles, which supply credentials without setting those variables.
    """
    import boto3
    import botocore.exceptions

    try:
        identity = boto3.client(
            "sts", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        ).get_caller_identity()
    except botocore.exceptions.NoCredentialsError:
        print("Error: No AWS credentials found.")
        print("Set them in .env, run `aws sso login`, or attach an instance/task role.")
        sys.exit(1)
    except botocore.exceptions.ClientError as e:
        print(f"Error: AWS rejected the credentials — {e.response['Error']['Code']}")
        print(e.response["Error"]["Message"])
        sys.exit(1)

    print(f"AWS account {identity['Account']} — {identity['Arn'].rsplit('/', 1)[-1]}")


def _open_browser():
    """Open browser after a short delay to let uvicorn finish binding."""
    time.sleep(1.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    _check_credentials()

    # Ensure the working directory is the project root so uvicorn can import server.py
    os.chdir(Path(__file__).parent)

    # This launcher serves plain HTTP on loopback, where a Secure cookie is
    # rejected by some browsers. Deployments run uvicorn directly and keep the
    # default of Secure=on.
    os.environ.setdefault("COOKIE_SECURE", "false")

    print(f"Starting AWS Cost Advisor at {URL}")
    print(f"Sign in as {os.environ.get('AUTH_USERNAME', 'Admin')}")
    print("Press Ctrl+C to stop.\n")

    threading.Thread(target=_open_browser, daemon=True).start()

    import uvicorn
    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="warning",
    )
