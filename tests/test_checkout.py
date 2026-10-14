import subprocess
from pathlib import Path

import pytest

from bau.checkout import Repos


def git(*args, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    src = tmp_path / "upstream"
    src.mkdir()
    git("init", "-b", "main", cwd=src)
    git("config", "user.email", "t@example.com", cwd=src)
    git("config", "user.name", "t", cwd=src)
    (src / "README.md").write_text("hello\n")
    git("add", ".", cwd=src)
    git("commit", "-m", "init", cwd=src)
    return src


def test_bare_clone_and_two_checkouts_share_objects(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    bare = repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    assert bare == tmp_path / "repos" / "demo.git" and (bare / "HEAD").exists()

    a = repos.create_checkout("nakomis/demo", "demo-1-first")
    b = repos.create_checkout("nakomis/demo", "demo-2-second")
    assert (a / "README.md").read_text() == "hello\n"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=a) == "demo-1-first"
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=b) == "demo-2-second"
    alternates = (a / ".git" / "objects" / "info" / "alternates").read_text().strip()
    assert alternates == str(bare / "objects")
    assert git("remote", "get-url", "origin", cwd=a) == str(upstream)

    (a / "a.txt").write_text("a\n")
    git("add", ".", cwd=a)
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "a", cwd=a)
    assert not (b / "a.txt").exists()


def test_fetch_rebase_and_diff_stats(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    wt = repos.create_checkout("nakomis/demo", "demo-3-rebase")
    (wt / "mine.txt").write_text("mine\n")
    git("add", ".", cwd=wt)
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "mine", cwd=wt)

    (upstream / "up.txt").write_text("up\n")
    git("add", ".", cwd=upstream)
    git("commit", "-m", "up", cwd=upstream)

    repos.fetch("nakomis/demo", token="")
    assert repos.rebase_onto_main("demo-3-rebase", token="") is True
    assert (wt / "up.txt").exists() and (wt / "mine.txt").exists()
    assert repos.diff_stats("demo-3-rebase") == (1, 1)


def test_rebase_conflict_is_reported_and_aborted(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    wt = repos.create_checkout("nakomis/demo", "demo-4-conflict")
    (wt / "README.md").write_text("mine\n")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-am", "mine", cwd=wt)
    (upstream / "README.md").write_text("theirs\n")
    git("commit", "-am", "theirs", cwd=upstream)
    repos.fetch("nakomis/demo", token="")
    assert repos.rebase_onto_main("demo-4-conflict", token="") is False
    assert git("status", "--porcelain", cwd=wt) == ""


def test_delete_checkout(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    wt = repos.create_checkout("nakomis/demo", "demo-5-del")
    repos.delete_checkout("demo-5-del")
    assert not wt.exists()
