from __future__ import annotations

import re
import subprocess

from bau.config import KeychainRef

_HEX = re.compile(r"^(?:[0-9a-f]{2})+$")


def _undo_security_hex(value: str) -> str:
    """macOS `security find-generic-password -w` prints a value that contains
    non-printable characters (a PEM key's newlines) as lowercase hex. Decode it
    only when it is hex AND decodes to multi-line UTF-8 text, so a single-line
    hex token (e.g. from `openssl rand -hex 32`) is returned unchanged."""
    if not _HEX.match(value):
        return value
    try:
        decoded = bytes.fromhex(value).decode("utf-8")
    except UnicodeDecodeError:
        return value
    return decoded if "\n" in decoded else value


def read_secret(ref: KeychainRef, runner=subprocess.run) -> str:
    r = runner(
        ["security", "find-generic-password", "-s", ref.service, "-a", ref.account, "-w"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"keychain item {ref.service}/{ref.account} not found")
    return _undo_security_hex(r.stdout.strip())
