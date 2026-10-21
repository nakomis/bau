import importlib
import sys
import types

import pytest

import bau.version


@pytest.fixture
def load_version(monkeypatch):
    """Reload bau.version against a given bau._build (None means the import fails).

    Teardown removes the fake build and reloads once more, so other tests see the
    unstamped module."""

    def load(build):
        monkeypatch.setitem(sys.modules, "bau._build", build)
        return importlib.reload(bau.version)

    yield load
    monkeypatch.undo()
    importlib.reload(bau.version)


def test_unstamped_checkout_reports_local(load_version):
    v = load_version(None)  # import fails
    assert (v.VERSION, v.GIT_SHA, v.image_tag()) == ("0.0.0+local", "", None)


def test_stamped_build_reports_its_stamp(load_version):
    v = load_version(types.SimpleNamespace(VERSION="0.4.2", GIT_SHA="abc1234"))
    assert (v.VERSION, v.GIT_SHA) == ("0.4.2", "abc1234")
    assert v.image_tag() == "0.4.2-abc1234"
