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
    missing = [v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY") if not os.environ.get(v)]
    if missing:
        print(f"Error: Missing AWS credentials: {', '.join(missing)}")
        print("Set them in .env or via export before running chat.py")
        sys.exit(1)


def _open_browser():
    """Open browser after a short delay to let uvicorn finish binding."""
    time.sleep(1.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    _check_credentials()

    # Ensure the working directory is the project root so uvicorn can import server.py
    os.chdir(Path(__file__).parent)

    print(f"Starting AWS Cost Advisor at {URL}")
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
