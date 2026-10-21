from pathlib import Path

import pytest

from bau.config import Config, load_config


@pytest.fixture(autouse=True)
def _bau_home(tmp_path: Path, monkeypatch):
    """No test depends on, or writes under, the developer's real BAU_HOME or home directory."""
    monkeypatch.setenv("BAU_HOME", str(tmp_path / "bau-home"))


def make_config(tmp_path: Path) -> Config:
    """The packaged defaults with every path pointed into tmp_path (logs included, so nothing
    reaches ~/Library)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "host.yaml").write_text(
        f"""paths:
  repos: {tmp_path}/repos
  work: {tmp_path}/work
  logs: {tmp_path}/logs
  snapshot: {tmp_path}/state.json
  pause_file: {tmp_path}/paused
  atc_cache: {tmp_path}/atc-cache.json
  lock: {tmp_path}/run.lock
"""
    )
    return load_config(config_dir)


@pytest.fixture
def cfg(tmp_path: Path):
    return make_config(tmp_path)
