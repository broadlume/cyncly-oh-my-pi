"""Per-repo agent container image resolution and build.

A target repo customizes its agent container by committing
``.robomp/Dockerfile.robomp`` on its config branch (``Settings.config_branch_for``,
defaulting to the repo's GitHub default branch). Contract: the file MUST begin
``ARG ROBOMP_BASE_IMAGE`` / ``FROM ${ROBOMP_BASE_IMAGE}``; the build context is
the repo's ``.robomp/`` directory from the config branch only. Repos without
the file run in the stock base image.

Trust note: the config branch is maintainer-controlled (never a PR head), so
build-time code execution is maintainer-trusted.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings
    from .github_client import RepoInfo
    from .sandbox import GitTransport

log = logging.getLogger("robomp.agent_image")

_DOCKERFILE_CONTRACT = (
    "the repo's .robomp/Dockerfile.robomp MUST begin with "
    "`ARG ROBOMP_BASE_IMAGE` / `FROM ${ROBOMP_BASE_IMAGE}`; the build context "
    "is the repo's .robomp/ directory from the config branch only"
)
_OUTPUT_TAIL = 4000

_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


class AgentImageBuildError(RuntimeError):
    """Per-repo agent image build failed; fails the task."""

    def __init__(self, *, repo: str, branch: str, detail: str) -> None:
        super().__init__(
            f"agent image build failed for {repo} (config branch {branch}): "
            f"{detail}\nContract: {_DOCKERFILE_CONTRACT}"
        )


def _slug(part: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", part.lower())


def _git(pool_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(pool_dir), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _build_lock(tag: str) -> threading.Lock:
    with _build_locks_guard:
        lock = _build_locks.get(tag)
        if lock is None:
            lock = threading.Lock()
            _build_locks[tag] = lock
        return lock


def _image_exists(tag: str) -> bool:
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return probe.returncode == 0


def _prune_stale_tags(repository: str, keep: str) -> None:
    """Best-effort removal of superseded tags for `repository`."""
    try:
        listing = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", repository],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for tag in listing.stdout.split():
            if tag and tag != keep:
                subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=60)
    except Exception:  # noqa: BLE001 - prune is strictly best-effort
        pass


def resolve_agent_image(
    *,
    settings: Settings,
    repo: RepoInfo,
    pool_dir: Path,
    git_transport: GitTransport,
) -> str:
    """Resolve (building if needed) the agent image for `repo`.

    Blocking; call from the worker thread. Returns the stock base image when
    the repo has no `.robomp/Dockerfile.robomp` on its config branch;
    otherwise a content-addressed per-repo tag. Raises `AgentImageBuildError`
    on build failure/timeout or a missing docker CLI.
    """
    base = settings.agent_base_image
    assert base
    branch = settings.config_branch_for(repo.full_name) or repo.default_branch

    try:
        git_transport.fetch_base_ref(repo=repo.full_name, pool_dir=pool_dir, ref=branch)
    except Exception as exc:  # noqa: BLE001 - pool already fetched for this task
        log.info("config-branch freshen failed (continuing)", extra={"repo": repo.full_name, "branch": branch, "err": str(exc)})

    tree = _git(pool_dir, "rev-parse", f"origin/{branch}:.robomp")
    if tree.returncode != 0:
        return base
    tree_hash = tree.stdout.strip()
    dockerfile = _git(pool_dir, "cat-file", "-e", f"origin/{branch}:.robomp/Dockerfile.robomp")
    if dockerfile.returncode != 0:
        return base

    owner, _, name = repo.full_name.partition("/")
    repository = f"robomp-agent/{_slug(owner)}-{_slug(name)}"
    tag = f"{repository}:{tree_hash[:12]}"

    if _image_exists(tag):
        return tag

    with _build_lock(tag):
        if _image_exists(tag):
            return tag
        with tempfile.TemporaryDirectory() as ctx:
            archive = subprocess.Popen(
                ["git", "-C", str(pool_dir), "archive", tree_hash],
                stdout=subprocess.PIPE,
            )
            untar = subprocess.Popen(["tar", "-x", "-C", ctx], stdin=archive.stdout)
            archive.stdout.close()  # type: ignore[union-attr]
            untar.wait()
            archive.wait()
            if archive.returncode != 0 or untar.returncode != 0:
                raise AgentImageBuildError(
                    repo=repo.full_name,
                    branch=branch,
                    detail=f"failed to export .robomp/ tree {tree_hash} from the pool clone",
                )
            log.info("building agent image", extra={"repo": repo.full_name, "branch": branch, "tag": tag})
            try:
                build = subprocess.run(
                    [
                        "docker", "build",
                        "--file", str(Path(ctx) / "Dockerfile.robomp"),
                        "--build-arg", f"ROBOMP_BASE_IMAGE={base}",
                        "--tag", tag,
                        ctx,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=settings.agent_image_build_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentImageBuildError(
                    repo=repo.full_name,
                    branch=branch,
                    detail=f"build timed out after {settings.agent_image_build_timeout_seconds:.0f}s",
                ) from exc
            except FileNotFoundError as exc:
                raise AgentImageBuildError(
                    repo=repo.full_name, branch=branch, detail="docker CLI not found"
                ) from exc
            output = f"{build.stdout}\n{build.stderr}"
            log.info("agent image build output", extra={"tag": tag, "output": output[-_OUTPUT_TAIL:]})
            if build.returncode != 0:
                raise AgentImageBuildError(
                    repo=repo.full_name,
                    branch=branch,
                    detail=f"docker build exited {build.returncode}:\n{output[-_OUTPUT_TAIL:]}",
                )
        _prune_stale_tags(repository, keep=tag)
        return tag
