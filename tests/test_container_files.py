import plistlib
import re
import subprocess
from pathlib import Path

import yaml

from bau.config import load_config

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "container" / "Dockerfile").read_text()

ALLOW_LIST = {
    r"^(api\.)?github\.com$",
    r"^objects\.githubusercontent\.com$",
    r"^codeload\.github\.com$",
    r"^api\.anthropic\.com$",
    r"^registry\.npmjs\.org$",
    r"^pypi\.org$",
    r"^files\.pythonhosted\.org$",
}


def test_claude_code_version_pinned():
    assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+(\s|$)", DOCKERFILE)


def test_npm_install_scripts_disabled():
    assert "npm config set ignore-scripts true" in DOCKERFILE


def test_runs_as_non_root_uid_501():
    assert re.search(r"useradd\b[^\n]*-u 501\b", DOCKERFILE)
    users = re.findall(r"^USER\s+(\S+)", DOCKERFILE, re.MULTILINE)
    assert users and users[-1] not in ("root", "0")


def test_filter_is_exactly_the_spec_allow_list():
    lines = [
        ln.strip()
        for ln in (ROOT / "container" / "filter").read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert set(lines) == ALLOW_LIST
    assert len(lines) == len(ALLOW_LIST)
    assert not any("nakomis" in ln for ln in lines)


def test_tinyproxy_denies_by_default():
    conf = (ROOT / "container" / "tinyproxy.conf").read_text()
    assert re.search(r"^FilterDefaultDeny\s+Yes", conf, re.MULTILINE)


def test_bau_net_is_internal_and_only_proxy_bridges():
    compose = yaml.safe_load((ROOT / "container" / "compose.yml").read_text())
    assert compose["networks"]["bau-net"]["internal"] is True
    assert "internal" not in (compose["networks"].get("egress") or {})
    for name, svc in compose["services"].items():
        nets = svc["networks"]
        if "egress" in nets:
            assert name == "bau-egress"
            assert "bau-net" in nets


def test_compose_network_name_matches_config():
    compose = yaml.safe_load((ROOT / "container" / "compose.yml").read_text())
    expected = load_config(ROOT / "config" / "bau.yaml").container.network
    assert compose["networks"]["bau-net"]["name"] == expected


def test_egress_image_built_from_pinned_alpine_and_no_latest():
    compose = yaml.safe_load((ROOT / "container" / "compose.yml").read_text())
    for svc in compose["services"].values():
        assert svc["build"]["dockerfile"] == "egress.Dockerfile"
        assert ":latest" not in svc["image"]
    egress = (ROOT / "container" / "egress.Dockerfile").read_text()
    assert re.search(r"^FROM alpine:\d+\.\d+", egress, re.MULTILINE)
    assert "apk add --no-cache tinyproxy" in egress
    for f in ("Dockerfile", "egress.Dockerfile", "compose.yml"):
        assert ":latest" not in (ROOT / "container" / f).read_text()


def test_tinyproxy_logs_to_stderr():
    conf = (ROOT / "container" / "tinyproxy.conf").read_text()
    assert re.search(r'^LogFile\s+"/dev/stderr"', conf, re.MULTILINE)


def test_dockerfile_has_no_uv_toml_noop():
    assert "uv.toml" not in DOCKERFILE


def test_every_plist_path_has_claude_and_homebrew():
    for f in sorted((ROOT / "launchd").glob("*.plist")):
        path = plistlib.loads(f.read_bytes())["EnvironmentVariables"]["PATH"].split(":")
        assert path[0] == "/Users/nakomis/.local/bin", f.name
        assert "/opt/homebrew/bin" in path, f.name
        env = plistlib.loads(f.read_bytes())["EnvironmentVariables"]
        assert env["PYTHONUNBUFFERED"] == "1", f.name


def _plist(name):
    with open(ROOT / "launchd" / f"com.nakomis.{name}.plist", "rb") as f:
        return plistlib.load(f)


def test_run_plist():
    p = _plist("bau-run")
    assert p["Label"] == "com.nakomis.bau-run"
    assert p["ProgramArguments"][-1] == "run"
    assert p["StartCalendarInterval"] == {"Hour": 1, "Minute": 0}


def test_snapshot_plist():
    p = _plist("bau-snapshot")
    assert p["Label"] == "com.nakomis.bau-snapshot"
    assert p["ProgramArguments"][-1] == "snapshot"
    assert p["StartCalendarInterval"] == [{"Hour": h, "Minute": 30} for h in range(7, 24)]
    assert len(p["StartCalendarInterval"]) == 17


def test_sweep_plist():
    p = _plist("bau-sweep")
    assert p["Label"] == "com.nakomis.bau-sweep"
    assert p["ProgramArguments"][-1] == "sweep"
    assert p["StartCalendarInterval"] == {"Weekday": 0, "Hour": 0, "Minute": 15}
    assert p["StandardOutPath"] == "/Users/nakomis/bau/logs/bau-sweep.log"
    assert p["EnvironmentVariables"]["DOCKER_HOST"] == "unix:///Users/nakomis/.colima/default/docker.sock"


def test_install_script_installs_every_plist():
    script = (ROOT / "install.sh").read_text()
    for f in (ROOT / "launchd").glob("*.plist"):
        assert f.stem.removeprefix("com.nakomis.") in script, f.name


def test_install_script_syntax():
    script = ROOT / "install.sh"
    assert script.read_text().startswith("#!/usr/bin/env bash")
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_install_script_builds_agent_image_with_load():
    # With a docker-container buildx builder current, a plain `docker build`
    # leaves the image in the build cache only; --load puts it in the local store.
    lines = [
        line
        for line in (ROOT / "install.sh").read_text().splitlines()
        if line.startswith("docker build") and "bau-agent:latest" in line
    ]
    assert len(lines) == 1
    assert "--load" in lines[0]
