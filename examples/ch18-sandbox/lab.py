"""Offline sandbox control-plane lab. Never executes supplied code on the host.

Python 3.11+ and a POSIX host. FakeRuntime is evidence injection, not isolation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from dataclasses import dataclass, field
from uuid import uuid4


class Denied(Exception):
    pass


class ArtifactRejected(Exception):
    pass


@dataclass(frozen=True)
class Context:
    """Created by a trusted host after authentication; never model arguments."""

    tenant_id: str
    user_id: str
    permissions: frozenset[str]


@dataclass(frozen=True)
class Profile:
    name: str = "analysis-v1"
    network: str = "none"
    root_read_only: bool = True
    user: str = "65532:65532"
    capabilities: tuple[str, ...] = ()
    no_new_privileges: bool = True
    cpu: float = 0.5
    memory_bytes: int = 128 * 1024 * 1024
    pids: int = 32
    wall_seconds: int = 5
    tmpfs_bytes: int = 8 * 1024 * 1024
    output_bytes: int = 4096
    output_files: int = 1
    host_mounts: tuple[str, ...] = ()


PROFILE = Profile()
PERMISSION = "sandbox:run:analysis"


@dataclass
class Task:
    task_id: str
    tenant_id: str
    user_id: str
    profile: Profile
    recipe: str
    status: str = "prepared"
    execution_outcome: str | None = None
    artifacts: list[dict[str, object]] = field(default_factory=list)


def prepare(context: Context, connection_tenant: str, arguments: dict) -> Task:
    if context.tenant_id != connection_tenant or PERMISSION not in context.permissions:
        raise Denied("tenant or permission mismatch")
    if not isinstance(arguments, dict) or set(arguments) != {"profile", "recipe"}:
        raise Denied("only profile and recipe are accepted")
    if arguments != {"profile": PROFILE.name, "recipe": "vpn-log-summary"}:
        raise Denied("profile or recipe is not admitted")
    return Task(str(uuid4()), context.tenant_id, context.user_id, PROFILE, arguments["recipe"])


def read_artifact(root: Path, relative_name: str, *, byte_limit: int) -> bytes:
    """Read one regular file without following links on a POSIX host.

    The producer must already be stopped. This small collector is not a general
    archive extractor or an adversarial live-filesystem snapshot implementation.
    """
    path = PurePosixPath(relative_name)
    if (not relative_name or path.is_absolute() or "\\" in relative_name
            or any(part in {"", ".", ".."} for part in relative_name.split("/"))):
        raise ArtifactRejected("non-canonical relative path")
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                          dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ArtifactRejected("only an unlinked-from-elsewhere regular file is accepted")
        if info.st_size > byte_limit:
            raise ArtifactRejected("artifact exceeds byte budget")
        chunks = []
        size = 0
        while chunk := os.read(file_fd, min(8192, byte_limit + 1 - size)):
            size += len(chunk)
            if size > byte_limit:
                raise ArtifactRejected("artifact grew past byte budget")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ArtifactRejected("artifact is missing or cannot be opened safely") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def collect_report(root: Path, profile: Profile) -> dict[str, object]:
    # Fixed manifest: no glob, recursive archive extraction, or model-selected path.
    try:
        # Stop after the first excess entry rather than enumerate an unbounded
        # producer-controlled directory into a set.
        names = []
        with os.scandir(root) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > profile.output_files:
                    raise ArtifactRejected("output file count exceeded")
        if names != ["report.json"]:
            raise ArtifactRejected("unexpected output entries or file count")
    except OSError as exc:
        raise ArtifactRejected("output directory unavailable") from exc
    raw = read_artifact(root, "report.json", byte_limit=profile.output_bytes)
    try:
        report = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ArtifactRejected("invalid report JSON") from exc
    if (not isinstance(report, dict) or set(report) != {"summary", "error_count"}
            or not isinstance(report["summary"], str) or len(report["summary"]) > 500
            or type(report["error_count"]) is not int or report["error_count"] < 0):
        raise ArtifactRejected("report schema mismatch")
    return {"name": "report.json", "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "content": report, "trust": "untrusted_content"}


@dataclass
class FakeRuntime:
    """Trusted test fixture. Its acknowledgments do not prove OS isolation."""

    outcome: str = "ok"
    stop_confirmed: bool = True
    destroy_confirmed: bool = True
    artifact: bytes = b'{"summary":"VPN certificate expired","error_count":2}'
    calls: list[str] = field(default_factory=list)

    def start(self, task: Task, output: Path) -> None:
        self.calls.append("start")
        (output / "report.json").write_bytes(self.artifact)

    def wait(self, wall_seconds: int) -> str:
        self.calls.append("wait")
        if self.outcome not in {"ok", "failed", "timed_out", "cancelled"}:
            raise ValueError("invalid fixture outcome")
        return self.outcome

    def stop(self) -> bool:
        self.calls.append("stop")
        return self.stop_confirmed

    def destroy(self) -> bool:
        self.calls.append("destroy")
        return self.destroy_confirmed


def execute(context: Context, task: Task, runtime: FakeRuntime, output: Path) -> Task:
    """Single-worker, in-memory orchestration. Never retries unknown work."""
    if ((context.tenant_id, context.user_id) != (task.tenant_id, task.user_id)
            or PERMISSION not in context.permissions):
        raise Denied("task owner or permission mismatch")
    if task.status != "prepared" or task.profile != PROFILE:
        raise Denied("task cannot be started or profile changed")
    task.status = "running"
    staged = None
    try:
        runtime.start(task, output)
        task.execution_outcome = runtime.wait(task.profile.wall_seconds)
    except Exception:
        task.execution_outcome = "unknown"
    try:
        stopped = runtime.stop()
    except Exception:
        stopped = False
    if not stopped:
        task.status = "cleanup_pending"
        return task
    if task.execution_outcome == "ok":
        try:
            staged = collect_report(output, task.profile)
        except ArtifactRejected:
            task.execution_outcome = "artifact_rejected"
        except Exception:
            # A collector bug must not bypass runtime destruction or publish a
            # previously staged artifact as success.
            task.execution_outcome = "artifact_collection_failed"
    try:
        destroyed = runtime.destroy()
    except Exception:
        destroyed = False
    if not destroyed:
        task.status = "cleanup_pending"
        return task
    if task.execution_outcome == "ok" and staged is not None:
        task.artifacts = [staged]
        task.status = "succeeded"
    else:
        task.status = task.execution_outcome or "unknown"
    return task


def demo() -> None:
    context = Context("tenant-a", "employee-7", frozenset({PERMISSION}))
    for scenario, runtime in (
        ("success", FakeRuntime()),
        ("failed", FakeRuntime(outcome="failed")),
        ("timeout", FakeRuntime(outcome="timed_out")),
        ("cancel", FakeRuntime(outcome="cancelled")),
        ("cleanup_unknown", FakeRuntime(destroy_confirmed=False)),
        ("invalid_artifact", FakeRuntime(artifact=b'{"summary":"bad"}')),
    ):
        task = prepare(context, "tenant-a", {"profile": "analysis-v1", "recipe": "vpn-log-summary"})
        with tempfile.TemporaryDirectory(prefix="ch18-demo-") as directory:
            execute(context, task, runtime, Path(directory))
        print(json.dumps({"scenario": scenario, "status": task.status,
                          "execution_outcome": task.execution_outcome,
                          "published_artifacts": len(task.artifacts),
                          "runtime_calls": runtime.calls}, ensure_ascii=False))


if __name__ == "__main__":
    demo()
