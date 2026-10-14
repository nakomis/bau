from __future__ import annotations

import re

from bau.config import Limits

TOO_BIG = re.compile(r"^BAU_TOO_BIG: (.+)$", re.MULTILINE)


def too_big_reason(result_text: str) -> str | None:
    m = TOO_BIG.search(result_text or "")
    return m.group(1).strip() if m else None


def diff_too_big(files: int, lines: int, limits: Limits) -> bool:
    return files > limits.too_big_files or lines > limits.too_big_lines
