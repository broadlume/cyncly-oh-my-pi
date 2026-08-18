"""Tests for per-repo agent image resolution against a real scratch git pool.

`docker` is stubbed with a PATH shim that records argv lines to a file and
exits per a control file, so no daemon is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from robomp import agent_image
from robomp.agent_image import AgentImageBuildError, resolve_agent_image
from robomp.github_client import RepoInfo

_DOCKERFILE = "ARG ROBOMP_BASE_IMAGE\nFROM ${ROBOMP_BASE_IMAGE}\nRUN touch /repo-marker\n"


class _FakeSettings:
    agent_base_image = "robomp:base"
    agent_image_build_timeout_seconds = 60.0

    @staticmethod
    def config_branch_for(repo_full_name: str) -> str | None:
        return None


class _NullTransport:
    def fetch_base_ref(self, *, repo: str, pool_dir: Path, ref: str) -> None:
        pass


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def pool(tmp_path: Path) -> Path:
    pool = tmp_path / "pool"
    pool.mkdir()
    _git(pool, "init", "-b", "main")
    _git(pool, "config", "user.email", "t@example.invalid")
    _git(pool, "config", "user.name", "t")
    (pool / ".robomp").mkdir()
    (pool / ".robomp" / "Dockerfile.robomp").write_text(_DOCKERFILE)
    _git(pool, "add", ".")
    _git(pool, "commit", "-m", "init")
    # resolve_agent_image reads `origin/<branch>`; alias it to local main.
    _git(pool, "update-ref", "refs/remotes/origin/main", "main")
    return pool


@pytest.fixture
def docker_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH shim: records argv (JSON lines) and exits per the control file."""
    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "docker-argv.jsonl"
    control = tmp_path / "docker-control.json"
    control.write_text(json.dumps({"image inspect": 1, "build": 0}))
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"argv_log = {str(argv_log)!r}\n"
        f"control_path = {str(control)!r}\n"
        "args = sys.argv[1:]\n"
        "with open(argv_log, 'a') as fh:\n"
        "    fh.write(json.dumps(args) + '\\n')\n"
        "control = json.load(open(control_path))\n"
        "for prefix, code in control.items():\n"
        "    if ' '.join(args).startswith(prefix):\n"
        "        sys.exit(code)\n"
        "sys.exit(0)\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(agent_image, "_build_locks", {})
    return tmp_path


def _argv_lines(shim_root: Path) -> list[list[str]]:
    log = shim_root / "docker-argv.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _repo() -> RepoInfo:
    return RepoInfo(
        full_name="Octo/Widget",
        default_branch="main",
        clone_url="https://github.com/octo/widget.git",
        private=False,
    )


def test_builds_tag_from_robomp_tree_hash(pool: Path, docker_shim: Path) -> None:
    tree_hash = _git(pool, "rev-parse", "main:.robomp")
    tag = resolve_agent_image(
        settings=_FakeSettings(), repo=_repo(), pool_dir=pool, git_transport=_NullTransport()
    )
    assert tag == f"robomp-agent/octo-widget:{tree_hash[:12]}"
    builds = [a for a in _argv_lines(docker_shim) if a[:1] == ["build"]]
    assert len(builds) == 1
    build = builds[0]
    assert "--build-arg" in build
    assert f"ROBOMP_BASE_IMAGE={_FakeSettings.agent_base_image}" in build
    file_arg = build[build.index("--file") + 1]
    assert file_arg.endswith("/Dockerfile.robomp")


def test_image_inspect_hit_short_circuits(pool: Path, docker_shim: Path) -> None:
    (docker_shim / "docker-control.json").write_text(json.dumps({"image inspect": 0}))
    tag = resolve_agent_image(
        settings=_FakeSettings(), repo=_repo(), pool_dir=pool, git_transport=_NullTransport()
    )
    assert tag.startswith("robomp-agent/octo-widget:")
    assert not [a for a in _argv_lines(docker_shim) if a[:1] == ["build"]]


def test_missing_robomp_dir_returns_base(tmp_path: Path, docker_shim: Path) -> None:
    pool = tmp_path / "bare-pool"
    pool.mkdir()
    _git(pool, "init", "-b", "main")
    _git(pool, "config", "user.email", "t@example.invalid")
    _git(pool, "config", "user.name", "t")
    (pool / "README.md").write_text("hi\n")
    _git(pool, "add", ".")
    _git(pool, "commit", "-m", "init")
    _git(pool, "update-ref", "refs/remotes/origin/main", "main")

    tag = resolve_agent_image(
        settings=_FakeSettings(), repo=_repo(), pool_dir=pool, git_transport=_NullTransport()
    )
    assert tag == _FakeSettings.agent_base_image
    assert _argv_lines(docker_shim) == []


def test_build_failure_raises(pool: Path, docker_shim: Path) -> None:
    (docker_shim / "docker-control.json").write_text(
        json.dumps({"image inspect": 1, "build": 1})
    )
    with pytest.raises(AgentImageBuildError, match="Octo/Widget"):
        resolve_agent_image(
            settings=_FakeSettings(), repo=_repo(), pool_dir=pool, git_transport=_NullTransport()
        )
