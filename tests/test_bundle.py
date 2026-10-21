import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
READS = (
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "bau",
    "launchd",
    "container/compose.yml",
    "scripts",
)

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build the bundle")


def run(cmd, cwd=REPO, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"{cmd} failed ({p.returncode}):\n{p.stdout}\n{p.stderr}")
    return p if not check else p.stdout


def repo_copy(dest: Path, dist: bool) -> Path:
    """Copy only what the script reads, never node_modules or .venv."""
    for rel in READS:
        src, dst = REPO / rel, dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "_build.py", "web_dist"))
        else:
            shutil.copy2(src, dst)
    if dist:
        (dest / "web/dist").mkdir(parents=True)
        (dest / "web/dist/index.html").write_text("<html>stub</html>")
    return dest


def test_bundle_installs_and_reports_its_version(tmp_path):
    repo = repo_copy(tmp_path / "repo", dist=True)
    pyproject = (repo / "pyproject.toml").read_text()
    out = tmp_path / "out"
    out.mkdir()
    printed = run([str(repo / "scripts/build-bundle.sh"), "9.9.9", "abc1234", str(out)])
    assert printed.strip() == str(out / "bau-9.9.9.tar.gz")
    with tarfile.open(out / "bau-9.9.9.tar.gz") as t:
        members = t.getmembers()
        names = {m.name for m in members}
        t.extractall(out, filter="data")
    d = out / "bau-9.9.9"
    assert {
        f"bau-9.9.9/{n}"
        for n in (
            "VERSION",
            "SHA256SUMS",
            "requirements.txt",
            "compose.yml",
            "INSTALL",
            "bau-9.9.9-py3-none-any.whl",
            "launchd/com.nakomis.bau-run.plist.in",
            "launchd/com.nakomis.bau-snapshot.plist.in",
            "launchd/com.nakomis.bau-api.plist.in",
        )
    } <= names
    assert {n.split("/")[0] for n in names} == {"bau-9.9.9"}
    # every launchd template in the repo is bundled, and nothing else
    bundled = {
        n.removeprefix("bau-9.9.9/launchd/")
        for n in names
        if n.startswith("bau-9.9.9/launchd/") and not n.endswith("/")
    }
    assert bundled == {p.name for p in (REPO / "launchd").glob("*.plist.in")}
    # SHA256SUMS covers every other file, and nothing stray (such as uv's .gitignore) is bundled
    files = {m.name.removeprefix("bau-9.9.9/") for m in members if m.isfile()}
    listed = {line.split("  ", 1)[1] for line in (d / "SHA256SUMS").read_text().splitlines()}
    assert listed == files - {"SHA256SUMS"}
    assert not any(f.startswith(".") for f in files)
    version = (d / "VERSION").read_text()
    assert "BAU_VERSION=9.9.9\nBAU_SHA=abc1234\n" in version
    assert "BAU_AGENT_IMAGE=registry.home.nakomis.com/nakomis/bau-agent:9.9.9-abc1234" in version
    assert "BAU_EGRESS_IMAGE=registry.home.nakomis.com/nakomis/bau-egress:9.9.9-abc1234" in version
    run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=d)
    for line in (d / "INSTALL").read_text().splitlines():
        run(["bash", "-c", line], cwd=d)
    assert run([str(d / "venv/bin/bau"), "version"]) == "bau 9.9.9 (abc1234)\n"
    assert next((d / "venv/lib").glob("python3.12/site-packages/bau/web_dist/index.html")).exists()
    # the checkout stays untouched
    assert (repo / "pyproject.toml").read_text() == pyproject
    assert not (repo / "bau/_build.py").exists()
    assert not (repo / "bau/web_dist").exists()


@pytest.mark.parametrize("version", ["not-a-version", "v1.2.3", "", "1.2.3 "])
def test_bundle_refuses_a_bad_version(tmp_path, version):
    repo = repo_copy(tmp_path / "repo", dist=True)
    p = run([str(repo / "scripts/build-bundle.sh"), version, "abc1234", str(tmp_path)], check=False)
    assert p.returncode != 0
    assert "version" in p.stderr.lower()
    assert not list(tmp_path.glob("*.tar.gz"))


@pytest.mark.parametrize("sha", ["", "abc123", "abc12345", "ABCDEFG", "abc123g"])
def test_bundle_refuses_a_bad_sha(tmp_path, sha):
    repo = repo_copy(tmp_path / "repo", dist=True)
    p = run([str(repo / "scripts/build-bundle.sh"), "1.2.3", sha, str(tmp_path)], check=False)
    assert p.returncode != 0
    assert "sha" in p.stderr.lower()
    assert not list(tmp_path.glob("*.tar.gz"))


def test_bundle_refuses_without_a_built_dashboard(tmp_path):
    repo = repo_copy(tmp_path / "repo", dist=False)
    out = tmp_path / "out"
    out.mkdir()
    p = run([str(repo / "scripts/build-bundle.sh"), "9.9.9", "abc1234", str(out)], check=False)
    assert p.returncode == 1
    assert "web/dist" in p.stderr
    assert not list(out.glob("*.tar.gz"))
