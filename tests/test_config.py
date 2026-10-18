from datetime import time
from pathlib import Path

import pytest

from bau.config import Limits, Nudge, ProjectConfig, load_config, load_projects, paused_reader

MINIMAL = """
paused: false
schedule: {no_new_sessions_after: "05:00", timezone: Europe/London}
limits: {pace_margin: 0.10, five_hour_kill: 0.80,
         session_wall_clock_minutes: 60, session_budget_usd: 5.0,
         fail_closed_sessions: 2, ci_fix_sessions_per_pr: 3,
         too_big_files: 15, too_big_lines: 400,
         sweep_session_wall_clock_minutes: 15, triage_budget_usd: 1.25,
         triage_wall_clock_minutes: 10, max_question_rounds: 4,
         sweep_owner_quiet_days: 7, sweep_batch_items: 40}
nudge: {prs_warn_count: 5, prs_warn_days: 5, prs_down_count: 10, prs_down_days: 14,
        questions_warn_count: 5, questions_warn_days: 3, questions_down_count: 10,
        questions_down_days: 7}
models: {default: claude-opus-5-5, fable: claude-fable-5-1, fable_label: BAU-fable}
identities: {github_login: nakomis, owner_plane_email: martin@nakomis.com,
             worker_plane_email: bau@nakomis.com, worker_github_login: "nakomis-bau[bot]"}
github_app: {app_id: 12345, installation_id: 67890,
             private_key: {service: bau, account: github-app-key}}
plane: {url: https://plane.home.nakomis.com, workspace: nakomis,
        api_key: {service: bau, account: plane-api-key},
        client_cert: ~/.config/bau/cere.crt.pem, client_key: ~/.config/bau/cere.key.pem}
claude: {oauth_token: {service: bau, account: claude-oauth-token}}
paths: {repos: /Users/nakomis/bau/repos, work: /Users/nakomis/bau/work,
        logs: /Users/nakomis/bau/logs, snapshot: /Users/nakomis/bau/state.json}
container: {image: bau-agent:latest, network: bau-net, proxy: http://bau-egress:8888}
"""


def test_loads_every_section(tmp_path: Path):
    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL)
    cfg = load_config(p)
    assert cfg.paused is False
    assert cfg.schedule.no_new_sessions_after == time(5, 0)
    assert cfg.limits.sweep_session_wall_clock_minutes == 15
    assert cfg.limits == Limits(0.10, 0.80, 60, 5.0, 2, 3, 15, 400, 15, 1.25, 10, 4, 7, 40)
    assert cfg.models.fable_label == "BAU-fable"
    assert cfg.github_app.private_key.account == "github-app-key"
    assert cfg.plane.client_cert == Path("~/.config/bau/cere.crt.pem").expanduser()
    assert cfg.paths.snapshot == Path("/Users/nakomis/bau/state.json")
    assert cfg.container.proxy == "http://bau-egress:8888"


def test_load_config_reads_nudge_and_new_limits(tmp_path: Path):
    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL)
    cfg = load_config(p)
    assert cfg.nudge == Nudge(5, 5, 10, 14, 5, 3, 10, 7)
    assert cfg.nudge.questions_warn_days == 3
    assert cfg.limits.max_question_rounds == 4
    assert cfg.limits.triage_budget_usd == 1.25
    assert not hasattr(cfg.limits, "open_pr_ceiling")


def test_load_projects_allows_null_repo(tmp_path: Path):
    p = tmp_path / "projects.yaml"
    p.write_text("projects:\n  NOREPO: {repo: null, deploys: false}\n")
    assert load_projects(p)["NOREPO"].repo is None


def test_missing_section_is_an_error(tmp_path: Path):
    p = tmp_path / "bau.yaml"
    p.write_text("paused: false\n")
    with pytest.raises(KeyError):
        load_config(p)


def test_projects_map_identifier_to_repo(tmp_path: Path):
    p = tmp_path / "projects.yaml"
    p.write_text(
        "projects:\n"
        "  HOME: {repo: nakomis/home-infra, deploys: true, deploy_action: 'Run the playbook'}\n"
        "  BAU: {repo: nakomis/bau, deploys: false}\n"
    )
    projects = load_projects(p)
    assert projects["HOME"] == ProjectConfig("HOME", "nakomis/home-infra", True, "Run the playbook")
    assert projects["BAU"].deploy_action == ""


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ('"yes"', False), ("1", False)])
def test_paused_is_only_a_literal_yaml_true(tmp_path: Path, value, expected):
    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL.replace("paused: false", f"paused: {value}"))
    assert load_config(p).paused is expected


def test_paused_reader_rereads_the_flag_and_keeps_the_last_value_on_a_bad_file(tmp_path: Path):
    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL)
    read = paused_reader(p, initial=False)
    assert read() is False
    p.write_text(MINIMAL.replace("paused: false", "paused: true"))
    assert read() is True
    p.write_text("paused: [unclosed\n")
    assert read() is True
    p.write_text(MINIMAL.replace("paused: false", "paused: 'true'"))
    assert read() is False
    p.unlink()
    assert read() is False


CODE_PROJECTS = {
    "ADMIN": "nakom-admin", "BAPP": "blog-app", "BOOT": "bootboots", "CHATS": "claude-chats",
    "CLOUD": "nakomis-infra", "CRYPT": "tinycrypt", "CSSC": "cdk-ssm-secret-checker",
    "CTHU": "cthulhu", "EYE": "mcps", "FLEET": "nakomis-fleet", "HAND": "handmade-intelligence",
    "KAPPA": "kappa-warmer", "KBRD": "mechanical-keyboard", "LAPC": "lapcat", "LIGHT": "lightning",
    "MUSH": "mushroom-humidor", "NAKIS": "nakom.is", "NAKTV": "naktv",
    "NDFM": "nakomis-dragonfruit-mcp", "PIOE": "epio", "PIPE": "blog-pipeline", "PISH": "pish",
    "PLAY": "playlist", "RECIP": "recipator", "SCHIM": "schimmelpfennig", "SCOPE": "nakoscope",
    "SCRUM": "nakomis-scrum", "SINT": "sinter", "STAT": "nakostat", "TVRM": "tv-remote",
}


def test_projects_yaml_covers_every_code_project():
    projects = load_projects(Path(__file__).parent.parent / "config" / "projects.yaml")
    assert {"BAU", "HOME"} <= projects.keys()
    for ident, repo in CODE_PROJECTS.items():
        assert projects[ident].repo == f"nakomis/{repo}", ident
    assert projects["PROJ"].repo is None
    assert projects["MULTI"].repo is None
    assert not {"BCON", "HARM", "MONEY", "BENCH"} & projects.keys()
    for p in projects.values():
        assert p.deploy_action if p.deploys else True, p.identifier


def test_projects_yaml_deploys_flags_match_the_workflows():
    projects = load_projects(Path(__file__).parent.parent / "config" / "projects.yaml")
    for ident in ("NAKIS", "SCRUM", "BAPP", "CRYPT", "RECIP", "HOME"):
        assert projects[ident].deploys, ident
    for ident in ("CTHU", "PISH", "BAU"):
        assert not projects[ident].deploys, ident
    assert "promote" in projects["BAPP"].deploy_action
    assert projects["SCRUM"].deploy_action == "Run the promote-prod workflow in GitHub Actions"
