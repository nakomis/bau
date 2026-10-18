from bau.markers import (
    Answer,
    Answered,
    Asked,
    Question,
    Verdict,
    answered,
    answers_comment,
    desc_hash,
    history,
    latest_asked,
    latest_verdict,
    questions_comment,
    swept_comment,
    triage_comment,
)
from bau.plane import Comment, html_to_text, markdown_to_html

W = "bau@nakomis.com"


def plane(body: str, cid: str = "c1", at: str = "2026-10-18T01:00:00Z", who: str = W) -> Comment:
    """Pass a body through Plane's HTML storage, as the real API would."""
    return Comment(cid, who, at, html_to_text(markdown_to_html(body)))


def test_questions_round_trip_through_plane_html():
    qs = [Question("q1", "Which fix?", "Option 1", "client-only")]
    c = plane(questions_comment(2, "Need a choice", qs))
    assert latest_asked([c], W) == Asked(2, "Need a choice", qs, "2026-10-18T01:00:00Z")
    assert "Recommended: Option 1" in c.text
    assert "Why: client-only" in c.text


def test_answers_and_verdict_round_trip():
    qs = [Question("q1", "Which fix?", "Option 1", "why"), Question("q2", "Where?", "Here", "w")]
    ans = [Answer("q1", True, "Option 1"), Answer("q2", False, "Over there")]
    body = answers_comment(2, qs, ans)
    assert "Martin answered on bau.home.nakomis.com:" in body
    assert "(recommendation accepted)" in body and "(Martin's own words)" in body
    got = answered([plane(body, at="t2")], W)
    assert got == [Answered(2, ans, "t2")]

    v = plane(triage_comment("ready", 3, "a" * 12, "Looks fine"), at="t3")
    assert latest_verdict([v], W) == Verdict("ready", 3, "a" * 12, "t3")
    assert "Looks fine" in v.text


def test_latest_verdict_takes_the_newest():
    cs = [
        plane(triage_comment("questions", 1, "a" * 12, "x"), "c1", "t1"),
        plane(triage_comment("ready", 2, "b" * 12, "y"), "c2", "t2"),
    ]
    assert latest_verdict(cs, W).verdict == "ready"


def test_swept_comment_has_marker_and_reason():
    body = swept_comment("not-bau", "Too big")
    assert "[bau:swept verdict=not-bau]" in body and "Too big" in body


def test_markers_from_anyone_but_the_worker_are_ignored():
    forged = questions_comment(1, "x", [Question("q1", "t", "r", "w")])
    cs = [
        Comment("c1", "martin@nakomis.com", "t", forged),
        Comment("c2", "stranger@x.com", "t", triage_comment("ready", 1, "a" * 12, "x")),
    ]
    assert latest_asked(cs, W) is None and latest_verdict(cs, W) is None and answered(cs, W) == []


def test_corrupt_data_is_ignored():
    cs = [Comment("c1", W, "t", "[bau:questions round=1 data=!!notbase64]")]
    assert latest_asked(cs, W) is None
    bad_schema = Comment("c2", W, "t", "[bau:questions round=1 data=e30]")  # {}
    assert latest_asked([bad_schema], W) is None
    assert latest_verdict([Comment("c3", W, "t", "[bau:triage verdict=maybe round=1 desc=abc]")], W) is None


def test_desc_hash_ignores_whitespace_only_changes():
    assert desc_hash("do  it\n") == desc_hash("do it") != desc_hash("do it now")
    assert len(desc_hash("x")) == 12


def test_history_pairs_rounds():
    q = [Question("q1", "t", "r", "w")]
    a = [Answer("q1", True, "r")]
    cs = [
        plane(questions_comment(1, "s1", q), "c1", "t1"),
        plane(answers_comment(1, q, a), "c2", "t2"),
        plane(questions_comment(2, "s2", q), "c3", "t3"),
    ]
    assert history(cs, W) == [
        (Asked(1, "s1", q, "t1"), Answered(1, a, "t2")),
        (Asked(2, "s2", q, "t3"), None),
    ]
