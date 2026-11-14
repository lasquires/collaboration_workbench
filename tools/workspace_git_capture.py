"""Bounded lexical evidence captured from immutable Git commit blobs."""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time


from tools.workspace_git_watch import GitWatchError


class GitCaptureError(GitWatchError):
    """A pinned commit could not be read or its Git protocol was invalid."""


_FULL_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_TREE_OUTPUT = 8 * 1024 * 1024


def _run(root: Path, args: tuple[str, ...], *, deadline: float, limit: int) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitCaptureError("overall capture deadline exceeded")
    argv = ["git", "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks",
            "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "core.quotePath=false",
            "-C", str(root), *args]
    output = [bytearray(), bytearray()]
    overflow = [False, False]
    proc = None
    readers = []
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def drain(stream, index):
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                room = max(0, limit + 1 - len(output[index]))
                if room:
                    output[index].extend(chunk[:room])
                if len(chunk) > room:
                    overflow[index] = True
        readers = [threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
                   threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True)]
        for reader in readers:
            reader.start()
        proc.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for reader in readers:
            reader.join(timeout=1)
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        raise GitCaptureError(f"git {args[0]} failed: {exc}") from exc
    for reader in readers:
        reader.join(timeout=max(0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        proc.stdout.close()
        proc.stderr.close()
        raise GitCaptureError("overall capture deadline exceeded while reading Git output")
    if proc.stdout:
        proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
    if overflow[0]:
        raise GitCaptureError(f"git {args[0]} output exceeded {limit} bytes")
    if proc.returncode:
        raise GitCaptureError(output[1].decode("utf-8", "replace").strip() or f"git {args[0]} failed")
    return bytes(output[0])


def _identifiers(body: bytes, budget: int, deadline: float) -> tuple[list[dict], bool]:
    text = body.decode("utf-8")
    if budget <= 0:
        return [], False
    tokens = []
    i = 0
    byte_offset = 0
    line, column = 1, 1
    while i < len(text):
        if time.monotonic() >= deadline:
            return tokens, False
        char = text[i]
        width = len(char.encode("utf-8"))
        if char == "\n":
            line, column = line + 1, 1
            byte_offset += width
            i += 1
            continue
        if char == "\r":
            byte_offset += width
            i += 1
            if i < len(text) and text[i] == "\n":
                byte_offset += 1
                i += 1
            line, column = line + 1, 1
            continue
        if char == "_" or (len(char) == 1 and char.isidentifier()):
            start_byte, start_line, start_column, start = byte_offset, line, column, i
            byte_offset += width
            i += 1
            column += 1
            while i < len(text) and (text[i] == "_" or ("A" + text[i]).isidentifier()):
                if time.monotonic() >= deadline:
                    return tokens, False
                byte_offset += len(text[i].encode("utf-8"))
                i += 1
                column += 1
            value = text[start:i]
            if value.isidentifier():
                if len(tokens) >= budget:
                    return tokens, False
                tokens.append({"identifier": value, "byte_offset": start_byte,
                               "byte_length": byte_offset - start_byte,
                               "line": start_line, "column": start_column})
            continue
        byte_offset += width
        column += 1
        i += 1
    return tokens, True



def _path_label(raw):
    try:return raw.decode('utf-8')
    except UnicodeDecodeError:return ''.join(chr(byte) if byte<128 and (chr(byte).isalnum() or chr(byte) in '/-._~') else '%'+format(byte,'02X') for byte in raw)


def _path_meta(raw,label,key='path'):
    return {key:label,key+'_git_hex':raw.hex()}


def capture_commit(root, commit, *, max_files=500, max_file_bytes=8*1024*1024,
                   max_total_bytes=32*1024*1024, max_tokens=20000, max_seconds=25.0):
    """Capture bounded source evidence from immutable regular-file blobs in a commit."""
    if not isinstance(commit, str) or not _FULL_COMMIT.fullmatch(commit):
        raise ValueError("commit must be a full lowercase SHA-1 or SHA-256 object ID")
    for name, value in (("max_files", max_files), ("max_file_bytes", max_file_bytes),
                        ("max_total_bytes", max_total_bytes), ("max_tokens", max_tokens)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if (not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool)
            or not math.isfinite(max_seconds) or max_seconds <= 0):
        raise ValueError("max_seconds must be finite and positive")
    deadline = time.monotonic() + max_seconds
    requested = Path(root).resolve()
    try:
        top = Path(os.fsdecode(_run(requested, ("rev-parse", "--show-toplevel"), deadline=deadline, limit=4096)).strip()).resolve()
        if top != requested:
            raise GitCaptureError("repository root does not match requested root")
        object_type = _run(top, ("cat-file", "-t", commit), deadline=deadline, limit=100).strip()
        if object_type != b"commit":
            raise GitCaptureError("requested object is not a commit")
        raw_tree = _run(top, ("ls-tree", "-r", "-l", "-z", "--full-tree", commit), deadline=deadline, limit=_MAX_TREE_OUTPUT)
    except GitCaptureError:
        raise
    except (OSError, ValueError) as exc:
        raise GitCaptureError(f"cannot inspect repository: {exc}") from exc
    entries = []
    for record in raw_tree.split(b"\0"):
        if not record:
            continue
        try:
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, oid, size_text = meta.decode("ascii").split()
            path = _path_label(raw_path)
            tree_size = None if size_text == "-" else int(size_text)
        except (ValueError, UnicodeError) as exc:
            raise GitCaptureError("malformed ls-tree output") from exc
        entries.append((raw_path, path, mode, kind, oid, tree_size))
    entries.sort(key=lambda entry: entry[0])
    files, excluded = [], []
    total_bytes = tokens_used = pinned_bytes = 0
    lexical_complete = True
    pinned_complete = True
    for index, (raw_path, path, mode, kind, oid, tree_size) in enumerate(entries):
        if time.monotonic() >= deadline:
            excluded.append({"reason": "deadline_exceeded", "count": len(entries) - index,
                             **_path_meta(raw_path,path,'first_path'), **_path_meta(entries[-1][0],entries[-1][1],'last_path')})
            pinned_complete = lexical_complete = False
            break
        if index >= max_files:
            excluded.append({"reason": "max_files", "count": len(entries) - index,
                             **_path_meta(raw_path,path,'first_path'), **_path_meta(entries[-1][0],entries[-1][1],'last_path')})
            pinned_complete = lexical_complete = False
            break
        item = {"path": path, "mode": mode, "blob": oid, "size": None,
                "availability": "excluded", "reason": None, "sha256": None, "body": None,
                "tokens": [], "text_state": None}
        item["git_path_hex"]=raw_path.hex()
        files.append(item)
        if mode not in ("100644", "100755") or kind != "blob":
            item["reason"] = "unsupported_git_entry"
            excluded.append({**_path_meta(raw_path,path), "reason": item["reason"]})
            pinned_complete = lexical_complete = False
            continue
        if tree_size is None:
            raise GitCaptureError(f"ls-tree omitted blob size for {path}")
        item["size"] = tree_size
        if tree_size > max_file_bytes or total_bytes + tree_size > max_total_bytes:
            reason = "max_file_bytes" if tree_size > max_file_bytes else "max_total_bytes"
            item["reason"] = reason
            excluded.append({**_path_meta(raw_path,path), "reason": reason})
            pinned_complete = lexical_complete = False
            continue
        try:
            blob = _run(top, ("cat-file", "blob", oid), deadline=deadline, limit=tree_size)
        except GitCaptureError:
            if time.monotonic() >= deadline:
                item["reason"] = "deadline_exceeded"
                excluded.append({"reason": "deadline_exceeded", "count": len(entries) - index,
                                 **_path_meta(raw_path,path,'first_path'), **_path_meta(entries[-1][0],entries[-1][1],'last_path')})
                pinned_complete = lexical_complete = False
                break
            raise
        size = len(blob)
        if size != tree_size:
            raise GitCaptureError(f"ls-tree blob size mismatch for {path}")
        item["sha256"] = hashlib.sha256(blob).hexdigest()
        git_hash = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        actual_oid = git_hash(b"blob " + str(size).encode("ascii") + b"\0" + blob).hexdigest()
        if actual_oid != oid:
            raise GitCaptureError(f"Git blob object ID mismatch for {path}")
        total_bytes += size
        pinned_bytes += size
        item["availability"], item["body"] = "available", blob
        if b"\0" in blob:
            item["text_state"] = "binary"
            continue
        try:
            blob.decode("utf-8")
        except UnicodeDecodeError:
            item["text_state"] = "binary"
            continue
        item["text_state"] = "utf8"
        remaining_tokens = max_tokens - tokens_used
        tokens, complete = _identifiers(blob, remaining_tokens, deadline)
        item["tokens"] = tokens
        tokens_used += len(tokens)
        if not complete:
            lexical_complete = False
            if time.monotonic() >= deadline:
                excluded.append({**_path_meta(raw_path,path), "reason": "deadline_exceeded_during_tokenization"})
            else:
                excluded.append({**_path_meta(raw_path,path), "reason": "max_tokens"})
    return {"format": "workbench-git-capture/v1", "commit": commit, "files": files,
            "coverage": {"complete": pinned_complete and lexical_complete, "pinned_complete": pinned_complete,
                         "lexical_complete": lexical_complete,
                         "lexical_scope": "UTF8 lexical identifiers; binary bytes pinned without interpretation",
                         "excluded": excluded,
                         "path_count": len(entries), "byte_count": pinned_bytes, "token_count": tokens_used}}






