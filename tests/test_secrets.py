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


def _returning(stdout):
    def fake_run(cmd, **kw):
        class R:
            returncode = 0

        R.stdout = stdout
        return R()

    return fake_run


PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"  # secret-check:allow (fake fixture)


def test_multi_line_value_returned_as_hex_is_decoded():
    # macOS `security -w` hex-encodes values containing newlines.
    hexed = PEM.encode().hex() + "\n"
    assert read_secret(KeychainRef("bau", "github-app-key"), runner=_returning(hexed)) == PEM


def test_single_line_hex_token_is_left_alone():
    token = "a3f9" * 16  # what `openssl rand -hex 32` produces
    assert read_secret(KeychainRef("bau", "proxy-token"), runner=_returning(token + "\n")) == token


def test_hex_that_decodes_to_one_line_text_is_left_alone():
    # "abcd" decodes to one-line bytes; only multi-line values are hex-encoded by security(1).
    assert read_secret(KeychainRef("bau", "x"), runner=_returning("61626364\n")) == "61626364"
