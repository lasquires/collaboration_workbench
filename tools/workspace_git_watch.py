"""Bounded, read-only Git working-tree snapshots for connected workspaces."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time


MAX_COMMAND_SECONDS = 10
MAX_STATUS_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024


class GitWatchError(RuntimeError):
    """Git watch could not inspect the requested repository."""


def _run(root: Path, *args: str, timeout: float = MAX_COMMAND_SECONDS,
         output_limit: int = 1024 * 1024, deadline: float | None = None) -> bytes:
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise GitWatchError("overall scan deadline exceeded")
    argv = ["git", "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "diff.external=",
            "-C", str(root), *args]
    captured = [bytearray(), bytearray()]
    overflow = [False, False]
    try:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def drain(stream, index):
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                room = max(0, output_limit + 1 - len(captured[index]))
                if room:
                    captured[index].extend(chunk[:room])
                if len(chunk) > room:
                    overflow[index] = True
        readers = [threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, 1), daemon=True)]
        for reader in readers:
            reader.start()
        process.wait(timeout=remaining)
        for reader in readers:
            reader.join(timeout=max(0, remaining))
        process.stdout.close()
        process.stderr.close()
    except (OSError, subprocess.TimeoutExpired) as exc:
        try:
            process.kill()
            process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()
        except (UnboundLocalError, OSError, subprocess.TimeoutExpired):
            pass
        raise GitWatchError(f"git {args[0]} failed: {exc}") from exc
    if overflow[0]:
        raise GitWatchError(f"git {args[0]} output exceeded {output_limit} bytes")
    if process.returncode:
        raise GitWatchError(captured[1].decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return bytes(captured[0])


def _git_path(root: Path, value: bytes) -> str:
    # Git quotes unusual paths unless core.quotePath is disabled; -z avoids it.
    try:return value.decode("utf-8")
    except UnicodeDecodeError:return "[Git path bytes "+value.hex()+"]"


def _safe_file(root: Path, rel: str, max_file_bytes: int, deadline: float) -> dict:
    path = root / rel
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return {"availability": "unavailable", "error": "path_resolves_outside_repository"}
        if path.is_symlink():
            return {"availability": "unavailable", "error": "symlink_not_read"}
        stat = path.stat()
        size = stat.st_size
        if size > max_file_bytes:
            return {"availability": "unavailable", "size": size, "error": "file_too_large"}
        digest = hashlib.sha256()
        read = 0
        with path.open("rb") as stream:
            while True:
                if time.monotonic() >= deadline:
                    return {"availability": "unavailable", "size": size, "error": "scan_deadline_exceeded"}
                chunk = stream.read(min(1024 * 1024, max_file_bytes + 1 - read))
                if not chunk:
                    break
                read += len(chunk)
                if read > max_file_bytes:
                    return {"availability": "unavailable", "size": size, "error": "file_too_large"}
                digest.update(chunk)
        return {"availability": "available", "size": read, "sha256": digest.hexdigest()}
    except FileNotFoundError:
        return {"availability": "unavailable", "error": "file_missing"}
    except OSError as exc:
        return {"availability": "unavailable", "error": f"read_error:{type(exc).__name__}"}


def snapshot_repository(root, max_file_bytes=64 * 1024 * 1024, *,
                        max_scan_seconds=30.0, max_status_bytes=MAX_STATUS_BYTES) -> dict:
    """Capture bounded repository state without changing the repository."""
    started = time.monotonic()
    deadline = started + max_scan_seconds
    if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes < 0:
        raise ValueError("max_file_bytes must be a non-negative integer")
    requested = Path(root).resolve()
    try:
        top = Path(os.fsdecode(_run(requested, "rev-parse", "--show-toplevel", deadline=deadline)).strip()).resolve()
    except GitWatchError as exc:
        payload = {"root": str(requested), "head": None, "branch": None, "index_identity": None,
                   "paths": [], "incomplete": True, "errors": [f"repository_metadata_unavailable:{exc}"]}
        payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return payload
    try:
        head = _run(top, "rev-parse", "--verify", "HEAD", timeout=3, deadline=deadline).decode("ascii", "replace").strip() or None
    except GitWatchError:
        head = None
    try:
        branch = _run(top, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=3, deadline=deadline).decode("utf-8", "replace").strip() or None
    except GitWatchError:
        branch = None
    index_path_out = _run(top, "rev-parse", "--git-path", "index", timeout=3, deadline=deadline)
    index_path = Path(os.fsdecode(index_path_out).strip())
    if not index_path.is_absolute():
        index_path = top / index_path
    try:
        digest = hashlib.sha256()
        index_size = index_path.stat().st_size
        if index_size > MAX_INDEX_BYTES:
            index_identity = None
            index_error = "index_too_large"
        else:
            with index_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("scan deadline")
                    digest.update(chunk)
            index_identity = digest.hexdigest()
            index_error = None
    except FileNotFoundError:
        index_identity = None
        index_error = None
    except OSError:
        index_identity = None
        index_error = "index_unavailable"
    except TimeoutError:
        index_identity = None
        index_error = "scan_deadline_exceeded"

    errors = []
    try:
        status = _run(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none",
                      output_limit=max_status_bytes, deadline=deadline)
        status_truncated = False
    except GitWatchError as exc:
        status, status_truncated = b"", True
        errors.append(str(exc))
    if index_error:
        errors.append(index_error)
    entries = []
    fields = status.split(b"\0")
    i = 0
    while i < len(fields) and fields[i]:
        item = fields[i]
        i += 1
        if len(item) < 4:
            continue
        xy, raw_path = item[:2].decode("ascii", "replace"), item[3:]
        if "R" in xy or "C" in xy:
            if i < len(fields):
                i += 1
        path = _git_path(top, raw_path)
        deleted = "D" in xy
        meta = {"path": path, "status": xy, "kind": "deleted" if deleted else ("untracked" if xy == "??" else "changed")}
        if not deleted:
            try:raw_path.decode("utf-8")
            except UnicodeDecodeError:
                meta["path_bytes_hex"]=raw_path.hex()
                meta["working"]={"availability":"unavailable","error":"non_utf8_worktree_path"}
            else:meta["working"] = _safe_file(top, path, max_file_bytes, deadline)
        entries.append(meta)
    entries.sort(key=lambda e: os.fsencode(e["path"]))
    incomplete = status_truncated or bool(index_error) or time.monotonic() >= deadline or any(
        e.get("working", {}).get("availability") == "unavailable" for e in entries)
    if time.monotonic() >= deadline:
        errors.append("overall_scan_deadline_exceeded")
    fingerprint_payload = {"head": head, "branch": branch, "index": index_identity, "paths": entries,
                          "incomplete": incomplete}
    fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, ensure_ascii=True, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
    return {"root": str(top), "head": head, "branch": branch, "index_identity": index_identity,
            "paths": entries, "fingerprint": fingerprint, "incomplete": incomplete,
            "errors": errors}


def compare_snapshots(previous: dict, current: dict) -> dict:
    """Return repository cursor changes and changed working paths."""
    errors = []
    prev_paths = {p["path"]: p for p in previous.get("paths", [])}
    cur_paths = {p["path"]: p for p in current.get("paths", [])}
    changed_set = {p for p in set(prev_paths) | set(cur_paths) if prev_paths.get(p) != cur_paths.get(p)}
    head_changed = previous.get("head") != current.get("head")
    roots_match = previous.get("root") == current.get("root")
    heads=[value for value in (previous.get("head"),current.get("head")) if value is not None]
    valid_heads=all(isinstance(value,str) and len(value) in (40,64) and all(c in '0123456789abcdef' for c in value) for value in heads)
    if not roots_match:
        errors.append("repository_scope_mismatch")
    elif not valid_heads:
        errors.append("committed_diff_unavailable:invalid_commit_identity")
    elif head_changed:
        try:
            if previous.get("head") and current.get("head"):
                args=("diff", "--name-only", "-z", previous["head"], current["head"], "--")
            else:
                args=("ls-tree", "-r", "--name-only", "-z", current.get("head") or previous["head"])
            raw = _run(Path(current["root"]), *args, output_limit=MAX_STATUS_BYTES)
            changed_set.update(_git_path(Path(current["root"]),part) for part in raw.split(b"\0") if part)
        except (GitWatchError, OSError) as exc:
            errors.append(f"committed_diff_unavailable:{exc}")
    changed = sorted(changed_set, key=os.fsencode)
    return {"head_changed": previous.get("head") != current.get("head"),
            "branch_changed": previous.get("branch") != current.get("branch"),
            "index_changed": previous.get("index_identity") != current.get("index_identity"),
            "changed_paths": changed,
            "incomplete": bool(previous.get("incomplete") or current.get("incomplete") or errors),
            "errors": errors,
            "fingerprint_changed": previous.get("fingerprint") != current.get("fingerprint")}
