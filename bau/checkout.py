from __future__ import annotations

import base64
import os
import shutil
import subprocess
from collections.abc import Callable
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

    def create_checkout(self, repo: str, branch: str) -> Path:
        bare = self.bare_path(repo)
        dest = self.checkout_path(branch)
        origin = self._git("remote", "get-url", "origin", cwd=bare).stdout.strip()
        self._git("clone", "--reference", str(bare), "--branch", "main", str(bare), str(dest))
        self._git("remote", "set-url", "origin", origin, cwd=dest)
        self._git("checkout", "-b", branch, cwd=dest)
        return dest

    def has_checkout(self, branch: str) -> bool:
        return self.checkout_path(branch).exists()

    def checkout_branch(self, repo: str, branch: str, token: str = "") -> Path:
        """Re-create a checkout of an already-pushed branch (e.g. for an open PR whose checkout is gone)."""
        bare = self.bare_path(repo)
        dest = self.checkout_path(branch)
        origin = self._git("remote", "get-url", "origin", cwd=bare).stdout.strip()
        self._git("clone", "--reference", str(bare), "--branch", "main", str(bare), str(dest))
        self._git("remote", "set-url", "origin", origin, cwd=dest)
        self._git(
            "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=dest, token=token
        )
        self._git("checkout", "-B", branch, f"origin/{branch}", cwd=dest)
        return dest

    def delete_checkout(self, branch: str) -> None:
        shutil.rmtree(self.checkout_path(branch), ignore_errors=True)

    def rebase_onto_main(self, branch: str, token: str) -> bool:
        wt = self.checkout_path(branch)
        self._git("fetch", "origin", "main", cwd=wt, token=token)
        result = self._git("rebase", "origin/main", cwd=wt, check=False)
        if result.returncode != 0:
            self._git("rebase", "--abort", cwd=wt, check=False)
            return False
        return True

    def diff_stats(self, branch: str) -> tuple[int, int]:
        wt = self.checkout_path(branch)
        out = self._git("diff", "--numstat", "origin/main...HEAD", cwd=wt).stdout.strip().splitlines()
        files = len(out)
        lines = 0
        for row in out:
            added, deleted, _ = row.split("\t", 2)
            lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
        return files, lines
