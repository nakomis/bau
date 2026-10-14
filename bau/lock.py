from __future__ import annotations

import fcntl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def run_lock(
    path: Path,
    wait_seconds: float = 0,
    poll_seconds: float = 30,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[bool]:
    """Exclusive lock. With wait_seconds == 0 it is non-blocking; otherwise it retries every
    poll_seconds until it succeeds or wait_seconds elapse. Yields False when it never got the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        deadline = monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    yield False
                    return
                sleep(min(poll_seconds, remaining))
        try:
            yield True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
