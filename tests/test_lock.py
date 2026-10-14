import fcntl
from pathlib import Path

from bau.lock import run_lock


class Clock:
    """Injected sleep and monotonic: sleeping advances time, nothing really waits."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def monotonic(self):
        return self.now


def hold(path: Path):
    f = open(path, "a")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def test_zero_wait_is_non_blocking(tmp_path):
    c = Clock()
    f = hold(tmp_path / "l")
    with run_lock(tmp_path / "l", sleep=c.sleep, monotonic=c.monotonic) as held:
        assert held is False
    f.close()
    assert c.sleeps == []


def test_free_lock_is_taken_without_sleeping(tmp_path):
    c = Clock()
    with run_lock(tmp_path / "l", wait_seconds=100, sleep=c.sleep, monotonic=c.monotonic) as held:
        assert held is True
    assert c.sleeps == []


def test_gives_up_after_the_wait(tmp_path):
    c = Clock()
    f = hold(tmp_path / "l")
    with run_lock(
        tmp_path / "l", wait_seconds=100, poll_seconds=30, sleep=c.sleep, monotonic=c.monotonic
    ) as held:
        assert held is False
    f.close()
    assert sum(c.sleeps) <= 100 and len(c.sleeps) >= 3
    assert c.now >= 100 or sum(c.sleeps) >= 90


def test_acquires_once_the_holder_lets_go(tmp_path):
    c = Clock()
    f = hold(tmp_path / "l")

    def sleep(s):
        c.sleep(s)
        if len(c.sleeps) == 2:
            f.close()

    with run_lock(
        tmp_path / "l", wait_seconds=3600, poll_seconds=30, sleep=sleep, monotonic=c.monotonic
    ) as held:
        assert held is True
    assert c.sleeps == [30, 30]
