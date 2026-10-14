from __future__ import annotations

import subprocess

from bau.config import KeychainRef


def read_secret(ref: KeychainRef, runner=subprocess.run) -> str:
    r = runner(
        ["security", "find-generic-password", "-s", ref.service, "-a", ref.account, "-w"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"keychain item {ref.service}/{ref.account} not found")
    return r.stdout.strip()
