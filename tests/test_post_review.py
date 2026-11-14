"""Posting the review: a 422 must not end with nothing posted and a success report.

GitHub refuses the whole review (422) when one inline comment targets a line it cannot
resolve. ``_post_review`` retries without the inline comments, which move into the body.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import pr_af.orchestrator as orch_mod
from pr_af.orchestrator import ReviewOrchestrator
from pr_af.schemas.output import GitHubComment, GitHubReview, review_without_inline_comments


def _status_error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/repos/o/r/pulls/1/reviews")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status, text=text, request=request))


def _review(event: str = "COMMENT") -> GitHubReview:
    comments = [
        GitHubComment(path="a.py", line=3, side="RIGHT", body="first finding"),
        GitHubComment(path="docs/x.md", line=9, side="RIGHT", body="second finding"),
    ]
    return GitHubReview(body="summary", event=event, comments=comments)


def _orchestrator(monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | None]) -> tuple[ReviewOrchestrator, list[GitHubReview]]:
    """An orchestrator whose GitHub client answers with `outcomes` in order (None = created)."""
    sent: list[GitHubReview] = []

    class _Client:
        async def post_review(self, *, review: GitHubReview, **_: object) -> dict:
            sent.append(review)
            outcome = outcomes[len(sent) - 1]
            if outcome is not None:
                raise outcome
            return {"id": 1}

    monkeypatch.setattr(orch_mod, "GitHubClient", _Client)
    orchestrator = ReviewOrchestrator.__new__(ReviewOrchestrator)
    orchestrator.pr_data = SimpleNamespace(owner="o", repo="r", number=1, head_sha="abc")
    return orchestrator, sent


def test_the_fallback_keeps_every_finding_in_the_body():
    fallback = review_without_inline_comments(_review())
    assert fallback.comments == []
    assert fallback.body.startswith("summary")
    for expected in ("`a.py:3`", "first finding", "`docs/x.md:9`", "second finding"):
        assert expected in fallback.body


def test_the_fallback_body_stays_under_githubs_limit():
    huge = GitHubReview(
        body="s",
        event="COMMENT",
        comments=[GitHubComment(path="a.py", line=1, side="RIGHT", body="x" * 100_000)],
    )
    assert len(review_without_inline_comments(huge).body) < 65_536


@pytest.mark.asyncio
async def test_a_clean_post_is_one_call(monkeypatch):
    orchestrator, sent = _orchestrator(monkeypatch, [None])
    assert await orchestrator._post_review(_review()) is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_422_on_a_line_retries_without_inline_comments(monkeypatch):
    line_error = _status_error(422, '{"message":"Unprocessable Entity","errors":["Line could not be resolved"]}')
    orchestrator, sent = _orchestrator(monkeypatch, [line_error, None])
    assert await orchestrator._post_review(_review()) is True
    assert len(sent[0].comments) == 2
    assert sent[1].comments == []
    assert "first finding" in sent[1].body and "second finding" in sent[1].body


@pytest.mark.asyncio
async def test_a_422_on_own_pr_downgrades_to_comment_and_keeps_the_inline_comments(monkeypatch):
    own_pr = _status_error(422, "Can not request changes on your own pull request")
    orchestrator, sent = _orchestrator(monkeypatch, [own_pr, None])
    assert await orchestrator._post_review(_review("REQUEST_CHANGES")) is True
    assert sent[1].event == "COMMENT"
    assert len(sent[1].comments) == 2


@pytest.mark.asyncio
async def test_own_pr_and_a_bad_line_take_both_fallbacks(monkeypatch):
    own_pr = _status_error(422, "Can not request changes on your own pull request")
    line_error = _status_error(422, "Line could not be resolved")
    orchestrator, sent = _orchestrator(monkeypatch, [own_pr, line_error, None])
    assert await orchestrator._post_review(_review("REQUEST_CHANGES")) is True
    assert [r.event for r in sent] == ["REQUEST_CHANGES", "COMMENT", "COMMENT"]
    assert sent[2].comments == []


@pytest.mark.asyncio
async def test_a_failed_retry_reports_not_posted(monkeypatch):
    line_error = _status_error(422, "Line could not be resolved")
    orchestrator, sent = _orchestrator(monkeypatch, [line_error, _status_error(422, "still refused")])
    assert await orchestrator._post_review(_review()) is False
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_non_422_error_is_not_retried(monkeypatch):
    orchestrator, sent = _orchestrator(monkeypatch, [_status_error(403, "forbidden")])
    assert await orchestrator._post_review(_review()) is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_network_error_reports_not_posted(monkeypatch):
    orchestrator, _ = _orchestrator(monkeypatch, [httpx.ConnectError("down")])
    assert await orchestrator._post_review(_review()) is False
