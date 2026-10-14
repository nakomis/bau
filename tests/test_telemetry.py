import json

from bau.telemetry import RateLimit, Window, parse_stream_line, rate_limit_from_event, result_hit_limit

EVENT = {
    "type": "rate_limit_event",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1792418400,
        "rateLimitType": "seven_day",
        "utilization": 0.54,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.55, "resetsAt": 1791994800},
            "seven_day": {"utilization": 0.54, "resetsAt": 1792418400},
        },
    },
}


def test_parses_both_windows():
    rl = rate_limit_from_event(EVENT)
    assert rl == RateLimit("allowed_warning", Window(0.55, 1791994800), Window(0.54, 1792418400))


def test_missing_window_is_none_not_zero():
    event = json.loads(json.dumps(EVENT))
    del event["rate_limit_info"]["unifiedWindows"]["seven_day"]
    rl = rate_limit_from_event(event)
    assert rl is not None and rl.seven_day is None and rl.five_hour == Window(0.55, 1791994800)


def test_non_rate_limit_event_is_none():
    assert rate_limit_from_event({"type": "assistant"}) is None


def test_parse_stream_line_tolerates_garbage():
    assert parse_stream_line("not json") is None
    assert parse_stream_line(json.dumps({"type": "result"})) == {"type": "result"}


def test_result_hit_limit_matches_the_documented_text():
    assert result_hit_limit({"is_error": True, "result": "You've hit your weekly limit · resets Mon 12:00am"})
    assert result_hit_limit({"is_error": True, "result": "You've hit your session limit · resets 3:45pm"})
    assert not result_hit_limit({"is_error": False, "result": "ok"})
    assert not result_hit_limit({"is_error": True, "result": "Not logged in · Please run /login"})


def test_malformed_window_fields_are_unknown_not_errors():
    event = json.loads(json.dumps(EVENT))
    event["rate_limit_info"]["unifiedWindows"]["five_hour"] = {"utilization": None, "resetsAt": 1}
    event["rate_limit_info"]["unifiedWindows"]["seven_day"] = {"utilization": 0.1, "resetsAt": "soon"}
    rl = rate_limit_from_event(event)
    assert rl is not None and rl.five_hour is None and rl.seven_day is None


def test_non_dict_windows_are_treated_as_empty():
    event = json.loads(json.dumps(EVENT))
    event["rate_limit_info"]["unifiedWindows"] = "nope"
    rl = rate_limit_from_event(event)
    assert rl == RateLimit("allowed_warning", None, None)


def test_non_dict_rate_limit_info_is_treated_as_empty():
    rl = rate_limit_from_event({"type": "rate_limit_event", "rate_limit_info": ["x"]})
    assert rl == RateLimit("unknown", None, None)


def test_non_finite_window_values_are_unknown():
    for bad in (
        {"utilization": float("nan"), "resetsAt": 1},
        {"utilization": float("inf"), "resetsAt": 1},
        {"utilization": 0.1, "resetsAt": float("inf")},
        {"utilization": 0.1, "resetsAt": float("nan")},
    ):
        event = json.loads(json.dumps(EVENT))
        event["rate_limit_info"]["unifiedWindows"]["seven_day"] = bad
        rl = rate_limit_from_event(event)
        assert rl is not None and rl.seven_day is None and rl.five_hour == Window(0.55, 1791994800)


def test_nan_from_the_stream_is_unknown():
    line = (
        '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":'
        '{"seven_day":{"utilization":NaN,"resetsAt":Infinity}}}}'
    )
    rl = rate_limit_from_event(parse_stream_line(line))
    assert rl is not None and rl.seven_day is None
