from bau.feedback import Feedback, ack_marker, pending_plane_comments, pending_threads
from bau.github import Thread, ThreadComment
from bau.plane import Comment

OWNER, WORKER = "nakomis", "nakomis-bau[bot]"


def t(id, resolved, *authors):
    return Thread(id, resolved, [ThreadComment(a, f"msg from {a}", "2026-10-16T00:00:00Z") for a in authors])


def test_unresolved_thread_with_owner_last_is_pending():
    assert pending_threads([t("a", False, OWNER)], OWNER, WORKER) == [t("a", False, OWNER)]


def test_thread_answered_by_worker_is_not_pending_until_owner_replies():
    assert pending_threads([t("a", False, OWNER, WORKER)], OWNER, WORKER) == []
    assert pending_threads([t("a", False, OWNER, WORKER, OWNER)], OWNER, WORKER) != []


def test_resolved_and_third_party_threads_are_ignored():
    assert pending_threads([t("a", True, OWNER), t("b", False, "greptile[bot]")], OWNER, WORKER) == []


def test_owner_done_thanks_is_still_pending_so_the_worker_resolves_it():
    thread = Thread("a", False, [ThreadComment(OWNER, "Done, thanks", "2026-10-16T00:00:00Z")])
    assert pending_threads([thread], OWNER, WORKER) == [thread]


def test_plane_comments_pending_until_acked():
    owner = Comment("c1", "martin@nakomis.com", "2026-10-16T00:00:00Z", "change approach")
    ack = Comment("c2", "bau@nakomis.com", "2026-10-16T01:00:00Z", f"Acknowledged {ack_marker('c1')}")
    assert pending_plane_comments([owner], "martin@nakomis.com", "bau@nakomis.com") == [owner]
    assert pending_plane_comments([owner, ack], "martin@nakomis.com", "bau@nakomis.com") == []
    later = Comment("c3", "martin@nakomis.com", "2026-10-16T02:00:00Z", "and this")
    assert pending_plane_comments([owner, ack, later], "martin@nakomis.com", "bau@nakomis.com") == [later]


def test_feedback_empty():
    assert Feedback([], [], False, False).empty
    assert not Feedback([], [], True, False).empty
