from datetime import time
from pathlib import Path

import pytest

import bau
from bau.config import AtcConfig, Limits, Nudge, bau_home, load_config, paused_reader


def test_bau_home_is_the_env_var_expanded_else_application_support(tmp_path, monkeypatch):
    monkeypatch.setenv("BAU_HOME", "~/somewhere")
    assert bau_home() == Path("~/somewhere").expanduser()
    monkeypatch.delenv("BAU_HOME")
    assert bau_home() == Path("~/Library/Application Support/BAU").expanduser()


def test_loads_every_section(tmp_path: Path, monkeypatch):
    home = tmp_path / "bau-home"
    cfg = load_config(tmp_path)
    assert not hasattr(cfg, "paused")
    assert cfg.schedule.no_new_sessions_after == time(5, 0)
    assert cfg.limits == Limits(0.10, 0.80, 60, 5.0, 2, 3, 15, 400, 15, 1.25, 10, 4, 7, 40, 2)
    assert cfg.nudge == Nudge(5, 5, 10, 14, 5, 3, 10, 7)
    assert cfg.models.fable_label == "BAU-fable"
    assert cfg.github_app.private_key.account == "github-app-key"
    assert cfg.plane.client_cert == home / "config" / "cere.crt.pem"
    assert cfg.plane.client_key == home / "config" / "cere.key.pem"
    assert cfg.paths.pause_file == home / "state" / "paused"
    assert cfg.paths.atc_cache == home / "state" / "atc-cache.json"
    assert cfg.paths.repos == home / "repos" and cfg.paths.work == home / "work"
    assert cfg.container.proxy == "http://bau-egress:8888"
    assert cfg.atc == AtcConfig("https://atc.plane.home.nakomis.com")


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n"])
def test_host_yaml_top_level_must_be_a_mapping(tmp_path, text):
    (tmp_path / "host.yaml").write_text(text)
    with pytest.raises(ValueError, match="top level must be a mapping"):
        load_config(tmp_path)


def test_empty_host_yaml_is_fine(tmp_path):
    (tmp_path / "host.yaml").write_text("")
    load_config(tmp_path)


def test_host_yaml_section_must_stay_a_mapping(tmp_path):
    (tmp_path / "host.yaml").write_text("paths:\n")
    with pytest.raises(ValueError, match="'paths' must be a mapping"):
        load_config(tmp_path)


def test_defaults_alone_load(tmp_path, monkeypatch):
    monkeypatch.setenv("BAU_HOME", str(tmp_path / "Application Support" / "BAU"))
    cfg = load_config(tmp_path)  # no host.yaml
    assert cfg.paths.snapshot == tmp_path / "Application Support" / "BAU" / "state" / "state.json"
    assert cfg.paths.lock.name == "run.lock" and cfg.paths.lock.parent == cfg.paths.snapshot.parent
    assert cfg.paths.logs == Path("~/Library/Logs/BAU").expanduser()


def test_overlay_merges_nested_keys(tmp_path):
    (tmp_path / "host.yaml").write_text("limits:\n  session_budget_usd: 9.5\n")
    cfg = load_config(tmp_path)
    assert cfg.limits.session_budget_usd == 9.5
    assert cfg.limits.too_big_files == 15  # sibling kept from the defaults


def test_overlay_paths_expand_bau_home_and_tilde(tmp_path):
    (tmp_path / "host.yaml").write_text(
        "paths:\n  repos: ${BAU_HOME}/elsewhere\n  work: ~/w\nplane:\n  client_cert: ${BAU_HOME}/c.pem\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.paths.repos == bau_home() / "elsewhere"
    assert cfg.paths.work == Path("~/w").expanduser()
    assert cfg.plane.client_cert == bau_home() / "c.pem"
    assert cfg.paths.snapshot == bau_home() / "state" / "state.json"  # sibling kept


def test_unknown_host_key_is_an_error(tmp_path):
    (tmp_path / "host.yaml").write_text("pathz:\n  repos: /x\n")
    with pytest.raises(ValueError, match="pathz"):
        load_config(tmp_path)


def test_container_image_follows_the_build_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr("bau.config.image_tag", lambda: "0.4.2-abc1234")
    assert (
        load_config(tmp_path).container.image == "registry.home.nakomis.com/nakomis/bau-agent:0.4.2-abc1234"
    )
    monkeypatch.setattr("bau.config.image_tag", lambda: None)
    assert load_config(tmp_path).container.image == "bau-agent:latest"


def test_host_yaml_may_override_the_image(tmp_path):
    (tmp_path / "host.yaml").write_text("container:\n  image: bau-agent:dev\n")
    assert load_config(tmp_path).container.image == "bau-agent:dev"


def test_nothing_shipped_names_a_home_directory():
    text = (Path(bau.__file__).parent / "defaults.yaml").read_text()
    assert "/Users/" not in text and "paused:" not in text


def test_paused_reader_is_the_pause_file_only(tmp_path: Path, capsys):
    flag = tmp_path / "paused"
    read = paused_reader(flag)
    assert read() is False
    flag.write_text("away until Monday\n")
    assert read() is True
    assert read() is True
    assert capsys.readouterr().err.count("away until Monday") == 1
    flag.write_text("")
    assert read() is True
    assert "(no reason given)" in capsys.readouterr().err
    flag.unlink()
    assert read() is False


def test_real_defaults_point_at_atc():
    cfg = load_config(Path(__file__).parent.parent / "config")
    assert cfg.atc.url == "https://atc.plane.home.nakomis.com"
    assert cfg.paths.atc_cache == bau_home() / "state" / "atc-cache.json"
