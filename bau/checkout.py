from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path


def _auth_args(token: str) -> list[str]:
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


class Repos:
    def __init__(self, repos_root: Path, work_root: Path):
        self.repos_root = Path(repos_root)
        self.work_root = Path(work_root)
        self.repos_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _git(
        *args: str, cwd: Path | None = None, token: str = "", check: bool = True
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *_auth_args(token), *args], cwd=cwd, check=check, capture_output=True, text=True
        )

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
