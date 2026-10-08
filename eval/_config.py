"""Shared configuration for the evaluation scripts.

These run as standalone subprocesses against a running stack, so they each need
the API's base URL. Docker Compose reads `.env` for its own interpolation, but a
shell does not — which made `python eval/run_all.py` silently hit the wrong port
unless the variable had been exported. Reading the same `.env` keeps one source
of truth for both.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


def load_env() -> None:
    """Seed os.environ from the repo's .env without overwriting real variables.

    An explicit environment variable always wins: a developer who exports
    EVAL_API_BASE for one run should not be overruled by the file.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def api_base() -> str:
    load_env()
    return os.getenv("EVAL_API_BASE", "http://localhost:8000")


def llm_base() -> str:
    load_env()
    return os.getenv("EVAL_LLM_BASE", "http://localhost:8001")


def use_utf8_console() -> None:
    """Make stdout/stderr UTF-8, whatever the console's default encoding is.

    The results carry ✓, ✗ and —. On Windows the default console encoding is
    cp1252, where those do not exist: the first one printed raises
    UnicodeEncodeError, the script dies mid-run, and run_all.py reports the
    phase as failed for a reason that has nothing to do with the system under
    test. Requiring PYTHONIOENCODING to be exported by hand hides that behind a
    step nobody remembers.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
