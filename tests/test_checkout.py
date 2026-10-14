import base64
import os
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


def test_repos_never_runs_git_inside_a_used_checkout():
    # The host must not run git in a directory a session has written (its .git/config is hostile).
    assert not hasattr(Repos, "rebase_onto_main")
    assert not hasattr(Repos, "diff_stats")


def _object_files(objects: Path) -> list[Path]:
    return [p for p in objects.rglob("*") if p.is_file() and p.parent.name != "info"]


@pytest.mark.parametrize("method", ["create_checkout", "checkout_branch"])
def test_checkouts_borrow_objects_by_alternates_never_by_hardlink(upstream: Path, tmp_path: Path, method):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    # file:// forces a real transfer, so the bare repo's own files start with one link each.
    bare = repos.ensure_bare("nakomis/demo", token="", origin_url=f"file://{upstream}")
    if method == "create_checkout":
        wt = repos.create_checkout("nakomis/demo", "demo-6-links")
    else:
        git("branch", "demo-6-links", "main", cwd=upstream)
        repos.fetch("nakomis/demo", token="")
        wt = repos.checkout_branch("nakomis/demo", "demo-6-links")
    files = _object_files(bare / "objects")
    assert files
    assert all(os.stat(f).st_nlink == 1 for f in files)
    alternates = (wt / ".git" / "objects" / "info" / "alternates").read_text().strip()
    assert alternates == str(bare / "objects")


def test_delete_checkout(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    wt = repos.create_checkout("nakomis/demo", "demo-5-del")
    repos.delete_checkout("demo-5-del")
    assert not wt.exists()


def test_token_reaches_git_only_via_environment_and_errors_do_not_leak_it(tmp_path: Path):
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, "", "")

    repos = Repos(tmp_path / "repos", tmp_path / "work", runner=fake_run)
    repos.ensure_bare("nakomis/demo", token="secret", origin_url="https://example.invalid/x.git")

    basic = base64.b64encode(b"x-access-token:secret").decode()
    first = calls[0]
    assert "secret" not in " ".join(first["argv"]) and basic not in " ".join(first["argv"])
    assert first["env"]["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"
    assert first["env"]["GIT_CONFIG_COUNT"] == "1" and first["env"]["GIT_CONFIG_KEY_0"] == "http.extraheader"
    for call in calls:
        assert "secret" not in " ".join(call["argv"]) and basic not in " ".join(call["argv"])


def test_failed_git_raises_runtime_error_without_token(tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    with pytest.raises(RuntimeError) as info:
        repos.ensure_bare("nakomis/demo", token="secret", origin_url=str(tmp_path / "missing"))
    msg = str(info.value)
    assert msg.startswith("git clone failed:")
    assert "secret" not in msg and "basic" not in msg


def test_checkout_branch_recreates_a_pushed_branch(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    git("checkout", "-b", "demo-9-pushed", cwd=upstream)
    (upstream / "pushed.txt").write_text("pushed\n")
    git("add", ".", cwd=upstream)
    git("commit", "-m", "pushed", cwd=upstream)
    git("checkout", "main", cwd=upstream)
    wt = repos.checkout_branch("nakomis/demo", "demo-9-pushed")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt) == "demo-9-pushed"
    assert (wt / "pushed.txt").read_text() == "pushed\n"
    assert git("remote", "get-url", "origin", cwd=wt) == str(upstream)


def test_checkout_branch_failure_leaves_no_partial_directory(upstream: Path, tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    repos.ensure_bare("nakomis/demo", token="", origin_url=str(upstream))
    with pytest.raises(RuntimeError):
        repos.checkout_branch("nakomis/demo", "demo-404-missing")
    assert not repos.checkout_path("demo-404-missing").exists()


@pytest.mark.parametrize("branch", ["../x", "/tmp/x", "a/b", "", "..", "a\\b", "x..y"])
def test_delete_checkout_refuses_escaping_names(tmp_path: Path, branch: str):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    sentinel = tmp_path / "x"
    sentinel.mkdir()
    (sentinel / "keep").write_text("1")
    with pytest.raises(ValueError):
        repos.delete_checkout(branch)
    assert (sentinel / "keep").exists() and (tmp_path / "work").exists()


def test_delete_checkout_removes_a_plain_branch(tmp_path: Path):
    repos = Repos(tmp_path / "repos", tmp_path / "work")
    (tmp_path / "work" / "home-1-x").mkdir()
    repos.delete_checkout("home-1-x")
    assert not (tmp_path / "work" / "home-1-x").exists()
