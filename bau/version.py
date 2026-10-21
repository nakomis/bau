"""Build identity. A CI build writes bau/_build.py with VERSION and GIT_SHA; a plain checkout has none."""

from __future__ import annotations

UNSTAMPED_VERSION = "0.0.0+local"

try:
    from bau._build import GIT_SHA, VERSION

    _STAMPED = True
except ImportError:
    VERSION, GIT_SHA = UNSTAMPED_VERSION, ""
    _STAMPED = False


def image_tag() -> str | None:
    """The container image tag for this build, or None when the checkout is unstamped."""
    return f"{VERSION}-{GIT_SHA}" if _STAMPED else None
