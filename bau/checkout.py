from __future__ import annotations

import base64
import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any


def _git_env(token: str) -> dict[str, str] | None:
    """Pass the token via GIT_CONFIG_* env vars so it never appears in argv or ps."""
    if not token:
        return None
    if "GIT_CONFIG_COUNT" in os.environ:
        raise RuntimeError("BAU does not support a caller-supplied GIT_CONFIG_COUNT")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


class Repos:
    def __init__(
        self,
        repos_root: Path,
        work_root: Path,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.repos_root = Path(repos_root)
        self.work_root = Path(work_root)
        self.runner = runner
        self.repos_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _git(
        self, *args: str, cwd: Path | None = None, token: str = "", check: bool = True
    ) -> subprocess.CompletedProcess:
        env: dict[str, Any] | None = _git_env(token)
        argv = ["git", *args]
        try:
            return self.runner(argv, cwd=cwd, check=check, capture_output=True, text=True, env=env)
        except subprocess.CalledProcessError as e:
            # Re-raise without argv, env or the original exception chain: either can carry the token.
            raise RuntimeError(f"git {args[0]} failed: {(e.stderr or '').strip()[:500]}") from None

    def bare_path(self, repo: str) -> Path:
        return self.repos_root / f"{repo.split('/', 1)[1]}.git"

    def checkout_path(self, branch: str) -> Path:
        return self.work_root / branch

    def ensure_bare(self, repo: str, token: str, origin_url: str | None = None) -> Path:
        bare = self.bare_path(repo)
        url = origin_url or f"https://github.com/{repo}.git"
        if not (bare / "HEAD").exists():
            self._git("clone", "--bare", url, str(bare), token=token)
            self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
            self._git("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*", cwd=bare)
        return bare

    def fetch(self, repo: str, token: str) -> None:
        self._git("fetch", "--prune", "origin", cwd=self.bare_path(repo), token=token)

    # Checkouts are always fresh clones made before a session starts. After a session has written to
    # one, the host never runs git inside it again: its .git/config could name hooks, fsmonitor or a
    # hostile remote. --shared borrows the bare repo's objects through alternates, so no object file
    # is hardlinked into a directory the container can write.
    def create_checkout(self, repo: str, branch: str) -> Path:
        bare = self.bare_path(repo)
        dest = self.checkout_path(branch)
        origin = self._git("remote", "get-url", "origin", cwd=bare).stdout.strip()
        self._git("clone", "--shared", "--branch", "main", str(bare), str(dest))
        self._git("remote", "set-url", "origin", origin, cwd=dest)
        self._git("checkout", "-b", branch, cwd=dest)
        return dest

    @contextlib.contextmanager
    def scratch_clone(self, repo: str) -> Iterator[Path]:
        """A throwaway clone of main for a read-only triage session; no token, deleted on exit.
        --no-hardlinks copies the objects: the bare repo is not mounted in a triage session (so no
        alternates), and no object file of the bare repo is linked into a directory the container
        can write."""
        dest = Path(tempfile.mkdtemp(prefix="triage-", dir=self.work_root))
        try:
            self._git("clone", "--no-hardlinks", "--branch", "main", str(self.bare_path(repo)), str(dest))
            yield dest
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def has_checkout(self, branch: str) -> bool:
        return self.checkout_path(branch).exists()

    def checkout_branch(self, repo: str, branch: str, token: str = "") -> Path:
        """Fresh clone of an already-pushed branch, made before every feedback session."""
        bare = self.bare_path(repo)
        dest = self.checkout_path(branch)
        origin = self._git("remote", "get-url", "origin", cwd=bare).stdout.strip()
        try:
            self._git("clone", "--shared", "--branch", "main", str(bare), str(dest))
            self._git("remote", "set-url", "origin", origin, cwd=dest)
            self._git(
                "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=dest, token=token
            )
            self._git("checkout", "-B", branch, f"origin/{branch}", cwd=dest)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return dest

    def delete_checkout(self, branch: str) -> None:
        if not branch or "/" in branch or "\\" in branch or ".." in branch:
            raise ValueError(f"refusing to delete checkout for branch {branch!r}")
        target = self.work_root / branch
        if target.resolve().parent != self.work_root.resolve():
            raise ValueError(f"refusing to delete checkout for branch {branch!r}")
        shutil.rmtree(target, ignore_errors=True)
