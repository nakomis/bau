import plistlib
import re
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
    expected = load_config(ROOT / "config").container.network
    assert compose["networks"]["bau-net"]["name"] == expected


def test_egress_image_built_from_pinned_alpine_and_no_latest():
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


def render(name, bau_home="/Users/x/Library/Application Support/BAU", home="/Users/x"):
    text = (ROOT / "launchd" / f"com.nakomis.{name}.plist.in").read_text()
    return plistlib.loads(text.replace("@BAU_HOME@", bau_home).replace("@HOME@", home).encode())


def test_rendered_plists_keep_the_space():
    for name in ("bau-run", "bau-snapshot", "bau-api"):
        p = render(name)
        assert p["ProgramArguments"][0] == "/Users/x/Library/Application Support/BAU/current/venv/bin/bau"
        assert p["EnvironmentVariables"]["BAU_HOME"] == "/Users/x/Library/Application Support/BAU"
        assert p["StandardOutPath"] == f"/Users/x/Library/Logs/BAU/{name}.log"
        assert p["StandardErrorPath"] == p["StandardOutPath"]
        assert p["EnvironmentVariables"]["DOCKER_HOST"] == "unix:///Users/x/.colima/default/docker.sock"


def test_templates_name_no_home_and_no_other_placeholder():
    templates = list((ROOT / "launchd").glob("*.plist.in"))
    assert len(templates) == 3
    for f in templates:
        text = f.read_text()
        assert "/Users/" not in text
        assert set(re.findall(r"@[A-Z_]+@", text)) <= {"@BAU_HOME@", "@HOME@"}


def test_only_templates_in_launchd_dir():
    assert not list((ROOT / "launchd").glob("*.plist"))


def test_every_plist_path_has_claude_and_homebrew():
    for name in ("bau-run", "bau-snapshot", "bau-api"):
        env = render(name)["EnvironmentVariables"]
        path = env["PATH"].split(":")
        assert path[0] == "/Users/x/.local/bin", name
        assert "/opt/homebrew/bin" in path, name
        assert env["PYTHONUNBUFFERED"] == "1", name


def test_run_plist():
    p = render("bau-run")
    assert p["Label"] == "com.nakomis.bau-run"
    assert p["ProgramArguments"][-1] == "run"
    assert p["StartCalendarInterval"] == {"Hour": 1, "Minute": 0}


def test_snapshot_plist():
    p = render("bau-snapshot")
    assert p["Label"] == "com.nakomis.bau-snapshot"
    assert p["ProgramArguments"][-1] == "snapshot"
    assert p["StartCalendarInterval"] == [{"Hour": h, "Minute": 30} for h in range(7, 24)]
    assert len(p["StartCalendarInterval"]) == 17


def test_api_plist_keeps_alive():
    p = render("bau-api")
    assert p["ProgramArguments"][-1] == "serve"
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True


def test_no_sweep_plist():
    assert not list((ROOT / "launchd").glob("com.nakomis.bau-sweep.plist*"))


def test_egress_config_is_baked_into_the_image():
    df = (ROOT / "container" / "egress.Dockerfile").read_text()
    assert "COPY tinyproxy.conf" in df and "COPY filter" in df
    svc = yaml.safe_load((ROOT / "container" / "compose.yml").read_text())["services"]["bau-egress"]
    assert "volumes" not in svc and "build" not in svc
    assert svc["image"].startswith("${BAU_EGRESS_IMAGE")
