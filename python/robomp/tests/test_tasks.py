import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from robomp import tasks
from robomp.github_client import IssueInfo, PullRequestInfo, RepoInfo


async def test_triage_issue_keeps_event_loop_live_while_workspace_setup_blocks(db, settings, monkeypatch, tmp_path):
    async def _resolve_repo_and_issue(_github, _payload):
        repo = RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://x/octo/widget.git",
            private=False,
        )
        issue = IssueInfo(
            repo="octo/widget",
            number=1,
            title="bug",
            body="b",
            state="open",
            author="alice",
            labels=(),
            is_pull_request=False,
        )
        return repo, issue

    monkeypatch.setattr(tasks, "_resolve_repo_and_issue", _resolve_repo_and_issue)

    async def _no_closing(*a, **k):
        return ()

    github = SimpleNamespace(list_closing_pull_requests=_no_closing)

    entered = threading.Event()
    release = threading.Event()
    captured: dict[str, object] = {}

    def _blocking_ensure(**_kwargs):
        entered.set()
        # True ONLY if a concurrent coroutine set `release` while we blocked here.
        # Blocks a WORKER THREAD (via to_thread) in the fixed code; blocks the
        # LOOP itself in the broken code.
        captured["release_seen_in_time"] = release.wait(1.0)
        return SimpleNamespace(branch="farm/x/y", session_dir=str(tmp_path / "sess"))

    sandbox = SimpleNamespace(natives_cache=None, ensure_workspace=_blocking_ensure)

    async def _noop_run_task(**_kwargs):
        return None

    monkeypatch.setattr(tasks, "run_task", _noop_run_task)

    async def _releaser():
        # Waits (off-loop) until ensure_workspace has actually started, then
        # releases it. This coroutine can ONLY make progress if the event loop
        # is live while ensure_workspace is blocking.
        await asyncio.to_thread(entered.wait, 1.0)
        assert entered.is_set(), "ensure_workspace never started"
        release.set()

    triage_task = asyncio.create_task(
        tasks.triage_issue(
            settings=settings,
            db=db,
            github=github,
            sandbox=sandbox,
            git_transport=SimpleNamespace(),
            payload={},
            delivery_id="d1",
        )
    )
    releaser_task = asyncio.create_task(_releaser())

    await asyncio.wait_for(triage_task, timeout=3.0)
    await asyncio.wait_for(releaser_task, timeout=1.0)

    assert captured.get("release_seen_in_time") is True, (
        "event loop was frozen during ensure_workspace: the concurrent releaser "
        "could not run, so release.wait timed out (this is the pre-fix hang)"
    )


async def test_run_workspace_op_drains_thread_before_propagating_cancel():
    started = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()

    def slow_op(**_kwargs):
        started.set()
        # Block on the worker thread until the test releases us.
        assert proceed.wait(2.0), "proceed was never set — test bug"
        finished.set()
        return "done"

    task = asyncio.create_task(tasks._run_workspace_op(slow_op))
    # Wait (off-loop) until the worker thread is actually running.
    await asyncio.to_thread(started.wait, 1.0)
    assert started.is_set()

    async def pump(turns: int = 20) -> None:
        # Deterministically advance the loop without a wall-clock sleep: each
        # sleep(0) drains the ready queue, so a DETACHING (pre-fix) helper would
        # resolve `task` within these turns. A draining helper keeps it pending
        # while the worker thread is still blocked on `proceed`.
        for _ in range(turns):
            await asyncio.sleep(0)

    # Cancel the AWAITING coroutine while the thread is mid-flight, then a SECOND
    # time while it is still blocked. The repeated cancel must land on the drain
    # loop's re-`await` and be swallowed by its `continue` branch, NOT abandon
    # the thread. The whole sequence runs under try/finally so any failed assert
    # still releases the worker and cannot leak a blocked thread into later tests.
    try:
        task.cancel()
        await pump()
        assert not task.done(), "helper propagated the first cancel before the thread completed (thread abandoned)"
        task.cancel()
        await pump()
        # The thread is still blocked on `proceed`, so it has not finished and
        # the task has not resolved despite two cancels.
        assert not finished.is_set(), "thread finished before we released it — impossible unless abandoned"
        assert not task.done(), "helper abandoned the thread after a repeated cancel"
    finally:
        proceed.set()

    # The helper must now let the thread finish, THEN raise CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await task
    # Deterministic in the fixed helper: the thread completed before the cancel propagated.
    assert finished.is_set(), "thread did not complete before cancellation propagated"


async def test_run_workspace_op_logs_worker_exception_on_concurrent_cancel(caplog):
    started = threading.Event()
    proceed = threading.Event()
    boom = RuntimeError("git exploded")

    def failing_op(**_kwargs):
        started.set()
        assert proceed.wait(2.0), "proceed was never set — test bug"
        raise boom

    task = asyncio.create_task(tasks._run_workspace_op(failing_op))
    await asyncio.to_thread(started.wait, 1.0)
    assert started.is_set()

    # Cancel the caller while the worker is still blocked (mid-flight), so the
    # helper enters its cancel-drain loop and is awaiting the shielded inner.
    task.cancel()
    await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="robomp.tasks"):
        # Release the worker so inner completes WITH an exception while the
        # helper is draining -> the drain's `await shield(inner)` re-raises boom,
        # breaks the loop, and the guarded log.warning must fire.
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "worker exception during cancel was not logged"
    assert any(r.exc_info and r.exc_info[1] is boom for r in warnings), (
        "the worker's exception was not attached to the warning"
    )


async def test_triage_issue_reopen_tears_down_finalized_workspace(db, settings, monkeypatch, tmp_path):
    """Re-triage of a finalized (reopened) issue must clear the stale workspace first.

    The prior branch was merged/deleted when the issue finalized, so a reopen has
    to branch afresh — mirroring the maintainer directive-reopen teardown.
    """

    async def _resolve_repo_and_issue(_github, _payload):
        repo = RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://x/octo/widget.git",
            private=False,
        )
        issue = IssueInfo(
            repo="octo/widget",
            number=1,
            title="bug",
            body="b",
            state="open",
            author="alice",
            labels=(),
            is_pull_request=False,
        )
        return repo, issue

    monkeypatch.setattr(tasks, "_resolve_repo_and_issue", _resolve_repo_and_issue)

    # The bot previously finalized this issue: a stale row + workspace exist.
    db.upsert_issue(key="octo/widget#1", repo="octo/widget", number=1, state="closed")

    calls: list[str] = []

    def _remove(**_kwargs):
        calls.append("remove")

    def _ensure(**_kwargs):
        calls.append("ensure")
        return SimpleNamespace(branch="farm/x/y", session_dir=str(tmp_path / "sess"))

    async def _fail_closing(*_a, **_k):
        raise AssertionError("closing-PR guard must not run when a DB row already exists")

    github = SimpleNamespace(list_closing_pull_requests=_fail_closing)
    sandbox = SimpleNamespace(natives_cache=None, ensure_workspace=_ensure, remove_workspace=_remove)

    async def _noop_run_task(**_kwargs):
        return None

    monkeypatch.setattr(tasks, "run_task", _noop_run_task)

    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload={},
        delivery_id="d1",
    )

    # Teardown must precede re-provisioning, and the row resets to a live state.
    assert calls == ["remove", "ensure"]
    row = db.get_issue("octo/widget#1")
    assert row is not None
    assert row.state == "reproducing"


# --- writable PR work via bot assignment -------------------------------------


def _pr_info(**overrides) -> PullRequestInfo:
    fields = {
        "repo": "octo/widget",
        "number": 7,
        "html_url": "https://github.com/octo/widget/pull/7",
        "head_ref": "feat/x",
        "base_ref": "main",
        "state": "open",
        "author": "alice",
        "head_repo": "octo/widget",
        "assignees": ("robomp-bot",),
    }
    fields.update(overrides)
    return PullRequestInfo(**fields)


def _pr_comment_payload(*, pr_number: int = 7) -> dict:
    """`issue_comment.created` on a PR, no maintainer directive attached."""
    return {
        "repository": {
            "full_name": "octo/widget",
            "default_branch": "main",
            "clone_url": "https://x/octo/widget.git",
            "private": False,
        },
        "issue": {
            "number": pr_number,
            "title": "add widget",
            "body": "pr body",
            "state": "open",
            "user": {"login": "alice"},
            "pull_request": {"url": f"https://api.github.com/repos/octo/widget/pulls/{pr_number}"},
        },
        "comment": {
            "id": 42,
            "body": "please tweak this",
            "created_at": "2026-01-01T00:00:00Z",
            "user": {"login": "alice"},
        },
    }


class _FakePRGitHub:
    """Minimal backend for the PR-conversation path; records posted comments."""

    def __init__(self, pr: PullRequestInfo) -> None:
        self.pr = pr
        self.comments: list[tuple[str, int, str]] = []

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        assert repo == "octo/widget"
        assert number == self.pr.number
        return self.pr

    async def get_repo(self, repo: str) -> RepoInfo:
        return RepoInfo(
            full_name=repo,
            default_branch="main",
            clone_url="https://x/octo/widget.git",
            private=False,
        )

    async def get_issue(self, repo: str, number: int) -> IssueInfo:
        return IssueInfo(
            repo=repo,
            number=number,
            title="add widget",
            body="pr body",
            state="open",
            author="alice",
            labels=(),
            is_pull_request=True,
        )

    async def post_comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((repo, number, body))


def _pr_conversation_harness(monkeypatch, tmp_path, *, head_ref: str = "feat/x"):
    """Stub run_task/_fetch_thread and hand back a recording sandbox."""
    captured: dict[str, object] = {}
    ensure_calls: list[dict] = []
    removed: list[int] = []

    def _ensure(**kwargs):
        ensure_calls.append(kwargs)
        return SimpleNamespace(branch=head_ref, session_dir=str(tmp_path / "sess"))

    def _remove(**kwargs):
        removed.append(int(kwargs["number"]))

    async def _fake_run_task(*, task_kind: str, inputs, **_kwargs):
        del inputs
        captured["task_kind"] = task_kind

    async def _no_thread(*_a, **_k):
        return ()

    monkeypatch.setattr(tasks, "run_task", _fake_run_task)
    monkeypatch.setattr(tasks, "_fetch_thread", _no_thread)

    sandbox = SimpleNamespace(natives_cache=None, ensure_workspace=_ensure, remove_workspace=_remove)
    return sandbox, captured, ensure_calls, removed


def test_can_handle_pr_directly_allows_assigned_non_author_same_repo(settings) -> None:
    assert (
        tasks._can_handle_pr_directly(settings=settings, repo_full="octo/widget", pr=_pr_info())
        is True
    )


def test_can_handle_pr_directly_rejects_assigned_fork_pr(settings) -> None:
    """The bot token cannot push to a fork, so assignment grants nothing there."""
    pr = _pr_info(head_repo="contrib/widget")
    assert tasks._can_handle_pr_directly(settings=settings, repo_full="octo/widget", pr=pr) is False


def test_can_handle_pr_directly_rejects_unassigned_foreign_pr(settings) -> None:
    pr = _pr_info(assignees=("bob",))
    assert tasks._can_handle_pr_directly(settings=settings, repo_full="octo/widget", pr=pr) is False


async def test_handle_pr_conversation_untracked_assigned_pr_creates_writable_row(
    db, settings, monkeypatch, tmp_path
) -> None:
    """An assigned contributor PR with no DB row gets a head-branch row lazily."""
    sandbox, captured, ensure_calls, _removed = _pr_conversation_harness(monkeypatch, tmp_path)

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=_FakePRGitHub(_pr_info()),
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload=_pr_comment_payload(),
        delivery_id="d-assigned",
    )

    assert [c["existing_branch"] for c in ensure_calls] == ["feat/x"]
    row = db.get_issue("octo/widget#7")
    assert row is not None
    assert (row.state, row.branch, row.pr_number) == ("opened", "feat/x", 7)
    assert captured["task_kind"] == "handle_comment"


async def test_handle_pr_conversation_reclaims_review_row_when_assigned(
    db, settings, monkeypatch, tmp_path
) -> None:
    """A leftover read-only review row is converted to the writable head branch."""
    db.upsert_issue(
        key="octo/widget#7",
        repo="octo/widget",
        number=7,
        state="reviewing",
        branch="review/pr-7",
        pr_number=7,
    )
    sandbox, captured, ensure_calls, removed = _pr_conversation_harness(monkeypatch, tmp_path)

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=_FakePRGitHub(_pr_info()),
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload=_pr_comment_payload(),
        delivery_id="d-reclaim",
    )

    # The detached review worktree cannot be re-pointed, so it is torn down first.
    assert removed == [7]
    row = db.get_issue("octo/widget#7")
    assert row is not None
    assert (row.state, row.branch) == ("opened", "feat/x")
    assert [c["existing_branch"] for c in ensure_calls] == ["feat/x"]
    assert captured["task_kind"] == "handle_comment"


async def test_handle_pr_conversation_keeps_skipping_review_row_when_not_assigned(
    db, settings, monkeypatch, tmp_path
) -> None:
    db.upsert_issue(
        key="octo/widget#7",
        repo="octo/widget",
        number=7,
        state="reviewing",
        branch="review/pr-7",
        pr_number=7,
    )
    sandbox, captured, ensure_calls, removed = _pr_conversation_harness(monkeypatch, tmp_path)

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=_FakePRGitHub(_pr_info(assignees=())),
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload=_pr_comment_payload(),
        delivery_id="d-no-reclaim",
    )

    assert removed == []
    assert ensure_calls == []
    assert "task_kind" not in captured
    row = db.get_issue("octo/widget#7")
    assert row is not None
    assert (row.state, row.branch) == ("reviewing", "review/pr-7")


async def test_handle_pr_conversation_revives_terminal_row_for_reopened_assigned_pr(
    db, settings, monkeypatch, tmp_path
) -> None:
    """No `reopened` handler exists; the fresh open PR state is what proves revival."""
    db.upsert_issue(
        key="octo/widget#7",
        repo="octo/widget",
        number=7,
        state="closed",
        branch="feat/x",
        pr_number=7,
    )
    sandbox, captured, ensure_calls, removed = _pr_conversation_harness(monkeypatch, tmp_path)
    github = _FakePRGitHub(_pr_info())

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload=_pr_comment_payload(),
        delivery_id="d-revive",
    )

    assert removed == [7]
    row = db.get_issue("octo/widget#7")
    assert row is not None
    assert (row.state, row.branch) == ("opened", "feat/x")
    assert [c["existing_branch"] for c in ensure_calls] == ["feat/x"]
    assert captured["task_kind"] == "handle_comment"
    assert github.comments == []


async def test_handle_pr_conversation_still_acks_terminal_row_for_closed_pr(
    db, settings, monkeypatch, tmp_path
) -> None:
    db.upsert_issue(
        key="octo/widget#7",
        repo="octo/widget",
        number=7,
        state="closed",
        branch="feat/x",
        pr_number=7,
    )
    sandbox, captured, ensure_calls, removed = _pr_conversation_harness(monkeypatch, tmp_path)
    github = _FakePRGitHub(_pr_info(state="closed"))

    await tasks.handle_pr_conversation(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=SimpleNamespace(),
        payload=_pr_comment_payload(),
        delivery_id="d-finalized",
    )

    assert removed == []
    assert ensure_calls == []
    assert "task_kind" not in captured
    assert [(repo, number) for repo, number, _body in github.comments] == [("octo/widget", 7)]


async def test_revoke_pr_assignment_removes_workspace_and_row(db, settings) -> None:
    db.upsert_issue(
        key="octo/widget#7",
        repo="octo/widget",
        number=7,
        state="opened",
        branch="feat/x",
        pr_number=7,
    )
    removed: list[int] = []
    sandbox = SimpleNamespace(remove_workspace=lambda **kwargs: removed.append(int(kwargs["number"])))

    await tasks.revoke_pr_assignment(
        settings=settings,
        db=db,
        sandbox=sandbox,
        payload={"repository": {"full_name": "octo/widget"}, "pull_request": {"number": 7}},
    )

    assert removed == [7]
    assert db.get_issue("octo/widget#7") is None


async def test_revoke_pr_assignment_without_row_is_noop(db, settings) -> None:
    def _boom(**_kwargs):
        raise AssertionError("no workspace should be touched when no row exists")

    await tasks.revoke_pr_assignment(
        settings=settings,
        db=db,
        sandbox=SimpleNamespace(remove_workspace=_boom),
        payload={"repository": {"full_name": "octo/widget"}, "pull_request": {"number": 7}},
    )

    assert db.get_issue("octo/widget#7") is None
