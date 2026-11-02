#!/usr/bin/env python3
"""Publish a bounded Obsidian learning folder to its dedicated Git repository.

Python 3.11+, standard library only. Local paths and operational state belong in
an external JSON configuration, never in the public repository.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit

CSS_TARGET = ".obsidian/snippets/agent-learning-entry-navigation.css"
PREFIX = "Agent 应用开发/"
MAX_FILE_BYTES = 16 * 1024 * 1024
TOKEN_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
]


class SyncError(RuntimeError):
    """A safe-to-display error; never embeds subprocess stderr or file content."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise SyncError(f"Cannot read valid JSON from {path.name}") from exc
    if not isinstance(result, dict):
        raise SyncError(f"Expected a JSON object in {path.name}")
    return result


@dataclass(frozen=True)
class Config:
    source_dir: Path
    css_source: Path
    repo_dir: Path
    remote_url: str
    state_dir: Path
    branch: str = "main"

    @classmethod
    def read(cls, path: Path) -> "Config":
        data = load_json(path)
        required = {"source_dir", "css_source", "repo_dir", "remote_url", "state_dir"}
        if not required <= data.keys():
            raise SyncError("Configuration is missing required fields")
        values = dict(data)
        for key in ("source_dir", "css_source", "repo_dir", "state_dir"):
            candidate = Path(values[key]).expanduser()
            if not candidate.is_absolute() or candidate.is_symlink():
                raise SyncError(f"{key} must be an absolute, non-symlink path")
            values[key] = candidate.resolve()
        try:
            result = cls(**values)
        except (ValueError, TypeError) as exc:
            raise SyncError("Configuration has invalid fields") from exc
        if result.branch != "main":
            raise SyncError("This dedicated publisher only operates on main")
        if not isinstance(result.remote_url, str) or not result.remote_url:
            raise SyncError("remote_url must be a nonempty string")
        parsed = urlsplit(result.remote_url)
        if parsed.password or parsed.username or "\n" in result.remote_url:
            raise SyncError("remote_url must not contain credentials")
        if result.repo_dir == result.source_dir or result.repo_dir in result.source_dir.parents or result.source_dir in result.repo_dir.parents:
            raise SyncError("Source and publication repository must be separate")
        if result.state_dir == result.repo_dir or result.repo_dir in result.state_dir.parents:
            raise SyncError("Operational state must remain outside the public repository")
        if path.resolve() == result.repo_dir or result.repo_dir in path.resolve().parents:
            raise SyncError("Local configuration must remain outside the public repository")
        return result


@contextmanager
def publisher_lock(cfg: Config):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    lock = cfg.state_dir / "publisher.lock"
    if lock.is_symlink():
        raise SyncError("Refusing a symlink lock file")
    with lock.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncError("Another publisher is already running") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save_status(cfg: Config, **changes) -> dict:
    target = cfg.state_dir / "status.json"
    result = load_json(target)
    result.update(changes)
    result["updated_at"] = now()
    atomic_write(target, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    return result


def safe_read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SyncError(f"Refusing non-regular source file: {path.name}")
    before = path.stat()
    if before.st_size == 0 or before.st_size > MAX_FILE_BYTES:
        raise SyncError(f"Source file is empty or too large: {path.name}")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise SyncError("Source changed during reading; waiting for a stable save")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncError(f"Source is not UTF-8 text: {path.name}") from exc
    if not text.strip():
        raise SyncError(f"Source file is blank: {path.name}")
    for pattern in TOKEN_PATTERNS:
        if pattern.search(text):
            raise SyncError(f"Potential credential detected; publication stopped: {path.name}")
    return data


@dataclass(frozen=True)
class Snapshot:
    files: dict[str, bytes]
    css: bytes
    digest: str


def capture(cfg: Config) -> Snapshot:
    if cfg.source_dir.is_symlink() or not cfg.source_dir.is_dir():
        raise SyncError("Source directory is unavailable; no files will be removed")
    def names():
        return sorted(p.name for p in cfg.source_dir.iterdir() if not p.name.startswith(".") and p.suffix in {".md", ".canvas"})
    first = names()
    if not first or not any(name.endswith(".md") for name in first):
        raise SyncError("Source folder has no notes; no files will be removed")
    files = {name: safe_read(cfg.source_dir / name) for name in first}
    css = safe_read(cfg.css_source)
    if names() != first:
        raise SyncError("Source filenames changed during reading; retry after saving")
    digest = hashlib.sha256()
    for name, content in files.items():
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    digest.update(css)
    return Snapshot(files, css, digest.hexdigest())


def heading_slug(heading: str) -> str:
    # GitHub-style Unicode headings: lowercase, remove punctuation, spaces to '-'.
    text = heading.strip().lower()
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"[*`~]", "", text)
    text = "".join(c for c in text if c in " _-" or not unicodedata.category(c).startswith(("P", "S", "C")))
    return text.replace(" ", "-")


def strip_private_sections(name: str, text: str) -> str:
    if name.startswith("09 "):
        text = re.sub(r"已有学习入口：[^\n]*", "", text)
    if name.startswith("24 "):
        text = re.sub(r"^已有笔记入口：[^\n]*\n?", "", text, flags=re.M)
    if name.startswith("92 "):
        for heading in ("关联已有笔记", "从已有知识中找案例"):
            text = re.sub(r"^## " + re.escape(heading) + r"\n.*?(?=^## |\Z)", "", text, flags=re.M | re.S)
        text = re.sub(r"^\s*- 'file\.inFolder\(\"Agent 应用开发\"\)'\n", "", text, flags=re.M)
        text = text.replace('path:"Agent 应用开发/" ', "")
    return text


def wiki_to_markdown(text: str, filename: str, files: dict[str, bytes]) -> tuple[str, int]:
    stems = {Path(name).stem: name for name in files}
    unknown = 0
    def replace(match):
        nonlocal unknown
        raw = match.group(1).replace("\\|", "|")
        target, separator, label = raw.partition("|")
        page, marker, anchor = target.partition("#")
        if page.startswith(PREFIX):
            page = page[len(PREFIX):]
        resolved = filename if not page else page if page in files else stems.get(page)
        if not resolved:
            unknown += 1
            return label if separator else page
        visible = label if separator else (Path(page).stem or anchor)
        # A link alias in a Markdown table must keep literal pipes escaped.
        visible = visible.replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")
        url = quote(resolved, safe="-._~")
        if marker:
            url += "#" + quote(anchor[1:] if anchor.startswith("^") else heading_slug(anchor), safe="-._~")
        return f"[{visible}]({url})"
    text = re.sub(r"!?\[\[([^\]]+)\]\]", replace, text)
    return text, unknown


def public_navigation(text: str) -> str:
    """Render the same two chapter links as a GitHub-readable navigation row."""
    def replace(match):
        lines = [line.removeprefix("> ").strip() for line in match.group(1).splitlines()]
        links = [line for line in lines if line and line != ">"]
        if len(links) != 2:
            raise SyncError("Chapter navigation must contain exactly two links")
        return f"| {links[0]} | {links[1]} |\n| :--- | ---: |\n"
    return re.sub(r"^> \[!agent-chapter-nav\]\n((?:>[^\n]*(?:\n|$))+)", replace, text, flags=re.M)


def has_page_heading(text: str) -> bool:
    """Recognize an H1 outside fenced examples, where # may be a comment."""
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        if fence_char:
            if re.fullmatch(r" {0,3}" + re.escape(fence_char) +
                            "{" + str(fence_length) + r",}[ \t]*", line):
                fence_char = ""
            continue
        fence = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            fence_char = fence[1][0]
            fence_length = len(fence[1])
        elif re.match(r"^# ", line):
            return True
    return False


def render(snapshot: Snapshot) -> tuple[dict[str, bytes], int]:
    output = {}
    unknown = 0
    for name, data in snapshot.files.items():
        text = data.decode("utf-8")
        if name.endswith(".canvas"):
            try:
                canvas = json.loads(text)
            except ValueError as exc:
                raise SyncError(f"Invalid Canvas JSON: {name}") from exc
            for node in canvas.get("nodes", []):
                if isinstance(node.get("text"), str):
                    node["text"] = node["text"].replace("[[" + PREFIX, "[[")
                if isinstance(node.get("file"), str) and node["file"].startswith(PREFIX):
                    node["file"] = node["file"][len(PREFIX):]
            text = json.dumps(canvas, ensure_ascii=False, indent="\t") + "\n"
        else:
            text = strip_private_sections(name, text)
            if name == "README.md" and text.startswith("---\n"):
                close = text.find("\n---\n", 3)
                if close < 0:
                    raise SyncError("Unterminated README frontmatter")
                text = text[close + 5:].lstrip("\n")
            text, stripped = wiki_to_markdown(text, name, snapshot.files)
            text = public_navigation(text)
            unknown += stripped
            # Keep YAML first while adding a browser-visible page heading.
            end = text.find("\n---\n", 3) + 5 if text.startswith("---\n") else 0
            if end == 4:
                raise SyncError(f"Unterminated frontmatter: {name}")
            if not has_page_heading(text[end:]):
                text = text[:end] + "\n# " + Path(name).stem + "\n\n" + text[end:].lstrip("\n")
        output[name] = text.encode("utf-8")
    output[CSS_TARGET] = snapshot.css
    return output, unknown


def valid_managed_path(name: str) -> bool:
    path = PurePosixPath(name)
    return name == CSS_TARGET or (len(path.parts) == 1 and not name.startswith(".") and path.suffix in {".md", ".canvas"})


def destination(cfg: Config, name: str) -> Path:
    if not valid_managed_path(name):
        raise SyncError("Manifest contains an out-of-scope publication path")
    path = cfg.repo_dir / name
    for part in [path, *path.parents]:
        if part == cfg.repo_dir.parent:
            break
        if part.is_symlink():
            raise SyncError("Refusing a symlink in the publication destination")
    if path.exists() and not path.is_file():
        raise SyncError("Publication destination is not a regular file")
    return path


def export(cfg: Config, snapshot: Snapshot | None = None) -> list[str]:
    snapshot = snapshot or capture(cfg)
    if not cfg.repo_dir.is_dir() or cfg.repo_dir.is_symlink():
        raise SyncError("Publication repository directory is unavailable")
    outputs, stripped = render(snapshot)
    manifest_path = cfg.state_dir / "manifest.json"
    previous = load_json(manifest_path).get("files", {})
    if not isinstance(previous, dict):
        raise SyncError("Publication manifest is invalid")
    planned = set(previous) | set(outputs)
    for name in planned:
        path = destination(cfg, name)
        if name not in previous and name in outputs and path.exists() and path.read_bytes() != outputs[name]:
            raise SyncError(f"Existing publication file is not managed; refusing to overwrite: {name}")
        if name in previous and path.exists() and sha(path.read_bytes()) != previous[name]:
            if name not in outputs or path.read_bytes() != outputs[name]:
                raise SyncError(f"Published copy has local edits; refusing to overwrite: {name}")
    changed = []
    for name, content in outputs.items():
        path = destination(cfg, name)
        if not path.exists() or path.read_bytes() != content:
            atomic_write(path, content)
            changed.append(name)
    for name in sorted(set(previous) - set(outputs)):
        path = destination(cfg, name)
        if path.exists():
            path.unlink()
            changed.append(name)
    payload = {"source_digest": snapshot.digest, "files": {n: sha(b) for n, b in outputs.items()}, "exported_at": now()}
    atomic_write(manifest_path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())
    save_status(cfg, last_export=now(), changed_files=sorted(changed), outside_links_as_text=stripped, error=None)
    return sorted(changed)


def git(cfg: Config, *args: str, check=True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GIT_PAGER": "cat"})
    try:
        result = subprocess.run(["git", "-C", str(cfg.repo_dir), *args], capture_output=True, text=True, env=env, timeout=90)
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise SyncError(f"Git {args[0]} could not finish") from exc
    if check and result.returncode:
        raise SyncError(f"Git {args[0]} failed (exit {result.returncode}); inspect local Git authentication, network and remote history")
    return result


def validate_git(cfg: Config) -> None:
    if not cfg.repo_dir.is_dir() or cfg.repo_dir.is_symlink():
        raise SyncError("Publication repository directory is unavailable")
    top = git(cfg, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != cfg.repo_dir:
        raise SyncError("Publication path is not the Git repository root")
    if git(cfg, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() != cfg.branch:
        raise SyncError("The publication checkout is not on the configured branch")
    if git(cfg, "remote", "get-url", "--all", "origin").stdout.splitlines() != [cfg.remote_url]:
        raise SyncError("Origin does not match the approved public repository")
    if git(cfg, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines() != [cfg.remote_url]:
        raise SyncError("Origin push destination does not match the approved public repository")
    for state in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge"):
        location = Path(git(cfg, "rev-parse", "--git-path", state).stdout.strip())
        if not location.is_absolute():
            location = cfg.repo_dir / location
        if location.exists():
            raise SyncError("Repository has an unfinished merge, rebase or replay")
    if git(cfg, "diff", "--name-only", "--diff-filter=U").stdout:
        raise SyncError("Repository contains unresolved conflicts")


def names_from_git(result: subprocess.CompletedProcess) -> set[str]:
    return {value for value in result.stdout.split("\0") if value}


def tracked_at_head(cfg: Config) -> set[str]:
    result = git(cfg, "ls-tree", "-r", "--name-only", "-z", "HEAD", check=False)
    return names_from_git(result) if result.returncode == 0 else set()


def verify_staged(cfg: Config, names: set[str]) -> None:
    """Check the actual index blobs, not only their possibly different worktree."""
    for name in names:
        index = git(cfg, "ls-files", "--stage", "--", name).stdout
        if not index:  # A staged deletion has no blob to publish.
            continue
        if index.split(" ", 1)[0] not in {"100644", "100755"}:
            raise SyncError("Refusing a staged symbolic link or submodule")
        size = int(git(cfg, "cat-file", "-s", f":{name}").stdout.strip())
        if size > MAX_FILE_BYTES:
            raise SyncError("A staged project file is too large")
        content = git(cfg, "show", f":{name}").stdout
        for pattern in TOKEN_PATTERNS:
            if pattern.search(content):
                raise SyncError("Potential credential detected in staged content; publication stopped")


def sync_once(cfg: Config, snapshot: Snapshot | None = None) -> dict:
    try:
        validate_git(cfg)
        snapshot = snapshot or capture(cfg)
        previous = load_json(cfg.state_dir / "manifest.json").get("files", {})
        managed = set(snapshot.files) | {CSS_TARGET} | set(previous)
        tracked = tracked_at_head(cfg)
        staged = names_from_git(git(cfg, "diff", "--cached", "--name-only", "-z"))
        if staged - managed - tracked:
            raise SyncError("Unexpected staged files are outside the publication project")
        verify_staged(cfg, staged)
        exported = export(cfg, snapshot)
        modified = names_from_git(git(cfg, "diff", "--name-only", "-z"))
        stage = {name for name in exported if (cfg.repo_dir / name).exists() or name in tracked or name in staged} | (modified & tracked)
        for name in stage:
            path = cfg.repo_dir / name
            if path.is_symlink():
                raise SyncError("Refusing to publish a symbolic link")
            if path.is_file():
                # Check tracked operational files as well as managed notes.
                safe_read(path)
        if stage:
            git(cfg, "add", "--", *sorted(stage))
        staged = names_from_git(git(cfg, "diff", "--cached", "--name-only", "-z"))
        if staged:
            verify_staged(cfg, staged)
            for name in staged:
                path = cfg.repo_dir / name
                if path.is_symlink():
                    raise SyncError("Refusing to publish a symbolic link")
                if path.is_file():
                    safe_read(path)
            git(cfg, "commit", "-m", "docs: sync learning notes")
            head = git(cfg, "rev-parse", "HEAD").stdout.strip()
            save_status(cfg, last_commit=now(), last_commit_sha=head, changed_files=sorted(staged))
        head_result = git(cfg, "rev-parse", "--verify", "HEAD", check=False)
        if head_result.returncode:
            return save_status(cfg, status="no_changes", error=None)
        head = head_result.stdout.strip()
        upstream = git(cfg, "rev-parse", "--verify", f"refs/remotes/origin/{cfg.branch}", check=False)
        if upstream.returncode or upstream.stdout.strip() != head:
            git(cfg, "push", "origin", cfg.branch)
            return save_status(cfg, status="synced", last_push=now(), last_pushed_commit=head, error=None)
        return save_status(cfg, status="no_changes" if not staged else "synced", last_pushed_commit=head, error=None)
    except SyncError as exc:
        save_status(cfg, status="error", error=str(exc), last_error=now())
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--export-only", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = Config.read(args.config)
        if args.status:
            print(json.dumps(load_json(cfg.state_dir / "status.json"), ensure_ascii=False, indent=2))
            return 0
        with publisher_lock(cfg):
            if args.export_only:
                changed = export(cfg)
                print(json.dumps({"exported_files": changed}, ensure_ascii=False))
            else:
                result = sync_once(cfg)
                print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SyncError, OSError) as exc:
        # OSError text can contain local file paths but never file contents/tokens.
        print(f"Publication stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
