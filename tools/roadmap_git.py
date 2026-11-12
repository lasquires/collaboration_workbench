"""Read-only, local Git provenance helpers for roadmap artifact references.

This module deliberately exposes no Git mutation or network operations. Paths in
source references are repository-relative POSIX paths, and revisions are
resolved to full commit IDs before content is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Optional


class GitAdapterError(Exception):
    """Base error for invalid Git adapter operations."""


class GitUnavailable(GitAdapterError):
    """The configured Git executable could not be started."""


class NotRepository(GitAdapterError):
    """The requested directory is not inside a Git work tree."""


class InvalidSource(GitAdapterError):
    """A revision, path, or line range is invalid or unavailable."""


@dataclass(frozen=True)
class RepositoryMetadata:
    root: str
    head_commit: Optional[str]
    branch: Optional[str]
    dirty: bool


@dataclass(frozen=True)
class GitSourceReference:
    repository_root: str
    commit: str
    path: str
    blob: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


def _run(git: str, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            [git, "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise GitUnavailable(f"Git executable not found: {git}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitAdapterError(f"Git command timed out after {exc.timeout} seconds") from exc
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise GitAdapterError(detail or f"git exited with status {result.returncode}")
    return result


def inspect_repository(directory: str | Path, *, git: str = "git") -> RepositoryMetadata:
    """Return local HEAD, branch and worktree dirtiness; raise NotRepository otherwise."""
    directory = Path(directory).resolve()
    root_result = _run(git, directory, "rev-parse", "--show-toplevel", check=False)
    if root_result.returncode:
        raise NotRepository(f"Not a Git work tree: {directory}")
    root = Path(root_result.stdout.decode("utf-8").strip()).resolve()
    head_result = _run(git, root, "rev-parse", "--verify", "HEAD", check=False)
    head = head_result.stdout.decode("ascii", "replace").strip() if not head_result.returncode else None
    branch_result = _run(git, root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = branch_result.stdout.decode("utf-8", "replace").strip() if not branch_result.returncode else None
    status = _run(git, root, "status", "--porcelain", "--untracked-files=normal")
    return RepositoryMetadata(str(root), head, branch, bool(status.stdout))


def _safe_path(file_path: str) -> str:
    if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
        raise InvalidSource("Path must be a non-empty repository-relative string")
    # Accept native Windows separators as input but canonicalize references.
    value = file_path.replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", value) or any(part in ("", ".", "..") for part in value.split("/")):
        raise InvalidSource("Path must stay within the repository and contain no traversal components")
    return path.as_posix()


def resolve_source(
    repository: str | Path,
    revision: str,
    file_path: str,
    *,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    git: str = "git",
) -> GitSourceReference:
    """Pin a repository-relative file at an immutable commit and return its blob ID.

    Line numbers are one-based and inclusive. A range must be supplied as a
    positive start and optional end greater than or equal to start. The path
    must exist as a blob at the selected commit. Invalid/unavailable references
    raise InvalidSource; non-repositories and missing Git have distinct errors.
    """
    if not isinstance(revision, str) or not revision or revision.startswith("-") or any(c.isspace() or ord(c) < 32 for c in revision):
        raise InvalidSource("Revision must be a non-option Git revision string")
    path = _safe_path(file_path)
    if (start_line is None) != (end_line is None) and end_line is not None:
        raise InvalidSource("Provide both line bounds, or only a start line")
    for line in (start_line, end_line):
        if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
            raise InvalidSource("Line numbers must be positive integers")
    if start_line is not None and end_line is not None and end_line < start_line:
        raise InvalidSource("End line must be greater than or equal to start line")

    repo = Path(repository).resolve()
    root_result = _run(git, repo, "rev-parse", "--show-toplevel", check=False)
    if root_result.returncode:
        raise NotRepository(f"Not a Git work tree: {repo}")
    root = Path(root_result.stdout.decode("utf-8").strip()).resolve()
    commit_result = _run(git, root, "rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
    if commit_result.returncode:
        raise InvalidSource(f"Revision does not resolve to a commit: {revision}")
    commit = commit_result.stdout.decode("ascii", "replace").strip()
    object_spec = f"{commit}:{path}"
    type_result = _run(git, root, "cat-file", "-t", object_spec, check=False)
    if type_result.returncode or type_result.stdout.strip() != b"blob":
        raise InvalidSource(f"Path is not a file at commit {commit}: {path}")
    blob_result = _run(git, root, "rev-parse", "--verify", object_spec, check=False)
    if blob_result.returncode:
        raise InvalidSource(f"Could not resolve file blob at commit {commit}: {path}")
    blob = blob_result.stdout.decode("ascii").strip()
    content_result = _run(git, root, "cat-file", "blob", blob)
    line_count = len(content_result.stdout.splitlines())
    if start_line is not None and start_line > line_count:
        raise InvalidSource(f"Start line {start_line} exceeds blob line count {line_count}")
    if end_line is not None and end_line > line_count:
        raise InvalidSource(f"End line {end_line} exceeds blob line count {line_count}")
    return GitSourceReference(str(root), commit, path, blob, start_line, end_line)
