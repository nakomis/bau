import pytest

from bau.config import KeychainRef
from bau.secrets import read_secret


def test_read_secret_calls_security():
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            returncode, stdout = 0, "s3cret\n"

        return R()

    assert read_secret(KeychainRef("bau", "plane-api-key"), runner=fake_run) == "s3cret"
    assert seen["cmd"] == ["security", "find-generic-password", "-s", "bau", "-a", "plane-api-key", "-w"]


def test_read_secret_missing_item_raises():
    def fake_run(cmd, **kw):
        class R:
            returncode, stdout = 44, ""

        return R()

    with pytest.raises(RuntimeError, match="bau/nope"):
        read_secret(KeychainRef("bau", "nope"), runner=fake_run)
