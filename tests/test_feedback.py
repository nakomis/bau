from bau.feedback import (
    Feedback,
    GhItem,
    ack_gh_marker,
    ack_marker,
    pending_gh_items,
    pending_plane_comments,
    pending_threads,
)
from bau.github import IssueComment, Review, Thread, ThreadComment
from bau.plane import Comment

# GraphQL reports an App's login without the "[bot]" suffix that REST and config use.
OWNER, WORKER, WORKER_CONFIG = "nakomis", "nakomis-bau", "nakomis-bau[bot]"


def t(id, resolved, *authors):
    return Thread(id, resolved, [ThreadComment(a, f"msg from {a}", "2026-10-16T00:00:00Z") for a in authors])


def test_unresolved_thread_with_owner_last_is_pending():
    assert pending_threads([t("a", False, OWNER)], OWNER, WORKER_CONFIG) == [t("a", False, OWNER)]


def test_thread_answered_by_worker_is_not_pending_until_owner_replies():
    assert pending_threads([t("a", False, OWNER, WORKER)], OWNER, WORKER_CONFIG) == []
    assert pending_threads([t("a", False, OWNER, WORKER, OWNER)], OWNER, WORKER_CONFIG) != []


def test_resolved_and_third_party_threads_are_ignored():
    assert pending_threads([t("a", True, OWNER), t("b", False, "greptile[bot]")], OWNER, WORKER_CONFIG) == []


def test_owner_done_thanks_is_still_pending_so_the_worker_resolves_it():
    thread = Thread("a", False, [ThreadComment(OWNER, "Done, thanks", "2026-10-16T00:00:00Z")])
    assert pending_threads([thread], OWNER, WORKER_CONFIG) == [thread]


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


def test_worker_login_matches_with_or_without_the_bot_suffix_on_either_side():
    answered = [t("a", False, OWNER, WORKER)]
    assert pending_threads(answered, OWNER, "nakomis-bau[bot]") == []
    assert pending_threads(answered, OWNER, "nakomis-bau") == []
    rest_shape = [t("a", False, OWNER, "nakomis-bau[bot]")]
    assert pending_threads(rest_shape, OWNER, "nakomis-bau") == []


def test_reply_marked_bau_counts_as_the_worker_whatever_the_login():
    thread = Thread(
        "a",
        False,
        [
            ThreadComment(OWNER, "rename this", "2026-10-16T00:00:00Z"),
            ThreadComment("someone-else", "[bau] Renamed in abc123.", "2026-10-16T01:00:00Z"),
        ],
    )
    assert pending_threads([thread], OWNER, WORKER_CONFIG) == []


def test_owner_issue_comment_and_review_body_are_pending():
    items = pending_gh_items(
        [IssueComment(1, OWNER, "please rename")],
        [Review(2, OWNER, "CHANGES_REQUESTED", "needs tests"), Review(3, OWNER, "COMMENTED", "nit")],
        OWNER,
        WORKER_CONFIG,
    )
    assert [i.key for i in items] == ["issue:1", "review:2", "review:3"]
    assert not Feedback(gh_items=items).empty


def test_acked_gh_items_are_not_pending_again():
    ack = f"[bau] Addressed. {ack_gh_marker('issue:1')} {ack_gh_marker('review:2')}"
    comments = [IssueComment(1, OWNER, "a"), IssueComment(5, WORKER_CONFIG, ack)]
    reviews = [Review(2, OWNER, "COMMENTED", "b"), Review(4, OWNER, "COMMENTED", "c")]
    assert [i.key for i in pending_gh_items(comments, reviews, OWNER, WORKER_CONFIG)] == ["review:4"]


def test_ack_marker_from_anyone_but_the_worker_does_not_count():
    forged = IssueComment(5, "mallory", f"[bau] Addressed. {ack_gh_marker('issue:1')}")
    items = pending_gh_items([IssueComment(1, OWNER, "a"), forged], [], OWNER, WORKER_CONFIG)
    assert [i.key for i in items] == ["issue:1"]


def test_other_authors_and_worker_comments_are_not_feedback():
    comments = [
        IssueComment(1, "mallory", "do evil"),
        IssueComment(2, WORKER, "[bau] hello"),
        IssueComment(3, OWNER, "[bau] pasted worker text"),
    ]
    reviews = [Review(4, "mallory", "COMMENTED", "evil"), Review(5, WORKER, "COMMENTED", "[bau] x")]
    assert pending_gh_items(comments, reviews, OWNER, WORKER_CONFIG) == []


def test_empty_body_and_approved_and_dismissed_reviews_are_ignored():
    reviews = [
        Review(1, OWNER, "COMMENTED", "  \n"),
        Review(2, OWNER, "APPROVED", "lgtm"),
        Review(3, OWNER, "DISMISSED", "old"),
        Review(4, OWNER, "PENDING", "draft"),
    ]
    assert pending_gh_items([], reviews, OWNER, WORKER_CONFIG) == []


def test_ghitem_key():
    assert GhItem("issue", 3, OWNER, "x").key == "issue:3"
