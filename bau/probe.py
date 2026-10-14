from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from bau.telemetry import RateLimit, parse_stream_line, rate_limit_from_event


def probe_usage(oauth_token: str, log: Callable[..., None] = print) -> RateLimit | None:
    """One-line session to read the current usage windows. Never --bare: that ignores the login."""
    cmd = [
        "claude",
        "-p",
        "Reply with exactly: ok",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-haiku-5-5",
        "--max-budget-usd",
        "0.05",
        "--disable-slash-commands",
    ]
    home = Path.home()
    env = {
        **os.environ,
        "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
        "HOME": str(home),
        "PATH": os.pathsep.join([str(home / ".local/bin"), os.environ.get("PATH", "")]),
    }
    try:
        proc = subprocess.run(cmd, input="", capture_output=True, text=True, env=env, timeout=120)
    except FileNotFoundError:
        log(
            f"PROBE FAILED: the claude CLI was not found on PATH={env['PATH']}; "
            "usage is unknown and the night runs fail-closed (telemetry: missing)"
        )
        return None
    except (OSError, subprocess.SubprocessError) as e:
        log(f"PROBE FAILED: {type(e).__name__}: {e}; telemetry: missing")
        return None
    latest = None
    for line in proc.stdout.splitlines():
        obj = parse_stream_line(line)
        if obj and (rl := rate_limit_from_event(obj)):
            latest = rl
    return latest
