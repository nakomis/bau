from pathlib import Path

import pytest

from bau.config import load_config


@pytest.fixture
def cfg(tmp_path: Path):
    from tests.test_config import MINIMAL

    p = tmp_path / "bau.yaml"
    p.write_text(MINIMAL.replace("/Users/nakomis/bau", str(tmp_path)))
    return load_config(p)
