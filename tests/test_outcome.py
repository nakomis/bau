from bau.config import Limits
from bau.outcome import diff_too_big, too_big_reason

LIMITS = Limits(50, 0.10, 0.80, 60, 5.0, 2, 3, 15, 400)


def test_too_big_reason_matches_only_a_whole_line():
    assert too_big_reason("did stuff\nBAU_TOO_BIG: needs a schema change\n") == "needs a schema change"
    assert too_big_reason("I almost wrote BAU_TOO_BIG: but no") is None
    assert too_big_reason("all good") is None


def test_diff_too_big_thresholds():
    assert diff_too_big(16, 10, LIMITS)
    assert diff_too_big(1, 401, LIMITS)
    assert not diff_too_big(15, 400, LIMITS)
