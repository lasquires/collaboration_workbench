"""Portable per-project Research Workspace contract helpers.

This module deliberately contains no app discovery or database access. Callers
must pass the connected project repository explicitly.
"""
from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROTOCOL = "research-workspace/v1"
PORTABLE_FORMAT = "research-workspace-project-schema"
_MODES = {"git-only", "hybrid", "live"}
_REF = re.compile(r"^refs/heads/workbench-data(?:/[A-Za-z0-9._-]+)*$")


def build_contract(schema_version, storage_ddl, portable_schema):
    """Build a JSON-ready project contract; database setup remains service-owned."""
    return {
        "contract_version": 1,
        "protocol": PROTOCOL,
        "schema": {
            "version": schema_version,
            "storage": "SQLite; private and service-owned",
            "ddl_reference": storage_ddl,
            "portable": {"format": PORTABLE_FORMAT, "schema": portable_schema},
            "initialization": "The setup command and service API initialize and migrate the database.",
        },
        "identity": {
            "project_id": "canonical UUID",
            "base_revision_id": "Explicit service revision (store UUID:counter) or snapshot SHA-256",
            "actor_and_time": "GitHub identity and timestamps are attribution metadata, not record keys.",
        },
        "synchronization": {
            "default_mode": "git-only",
            "data_ref": "refs/heads/workbench-data",
            "database_transport": "Never merge or push SQLite database files.",
        },
    }


def validate_manifest(doc):
    """Validate current and legacy project manifests, returning normalized data."""
    if not isinstance(doc, dict) or not {"protocol", "project_id", "name"}.issubset(doc):
        raise ValueError("Malformed project manifest")
    if set(doc) - {"protocol", "project_id", "name", "synchronization"}:
        raise ValueError("Unknown manifest fields")
    if doc["protocol"] != PROTOCOL:
        raise ValueError("Unsupported project protocol")
    try:
        pid = doc["project_id"]
        if not isinstance(pid, str) or str(uuid.UUID(pid)) != pid:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ValueError("Project ID must be a canonical UUID") from None
    name = doc["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError("Project name must contain 1 to 200 characters")
    result = {"protocol": PROTOCOL, "project_id": pid, "name": name}
    if "synchronization" not in doc:
        return result
    sync = doc["synchronization"]
    allowed = {"mode", "remote_name", "repository", "data_ref", "min_interval_seconds", "quiet_seconds"}
    if not isinstance(sync, dict) or set(sync) - allowed:
        raise ValueError("Malformed synchronization settings")
    mode = sync.get("mode", "git-only")
    remote = sync.get("remote_name")
    repository = sync.get("repository")
    ref = sync.get("data_ref", "refs/heads/workbench-data")
    interval = sync.get("min_interval_seconds", 300)
    quiet = sync.get("quiet_seconds", 30)
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("Unsupported synchronization mode")
    if remote is not None and (not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", remote)):
        raise ValueError("Invalid Git remote name")
    if repository is not None and (not isinstance(repository, str) or len(repository) > 2048
                                   or _has_credentials(repository) or _is_local_path(repository)):
        raise ValueError("Repository identity must be credential-free")
    if not isinstance(ref, str) or not _REF.fullmatch(ref) or ".." in ref or "@{" in ref or any(part.startswith(".") or part.endswith((".",".lock")) for part in ref.split("/")):
        raise ValueError("Data ref must be under refs/heads/workbench-data")
    if type(interval) is not int or not 30 <= interval <= 86400:
        raise ValueError("Minimum interval must be 30 to 86400 seconds")
    if type(quiet) is not int or not 0 <= quiet <= 3600:
        raise ValueError("Quiet period must be 0 to 3600 seconds")
    result["synchronization"] = {"mode": mode, "remote_name": remote, "repository": repository,
        "data_ref": ref, "min_interval_seconds": interval, "quiet_seconds": quiet}
    return result


def _has_credentials(value):
    try:
        parsed = urlsplit(value)
        return parsed.username is not None or parsed.password is not None
    except ValueError:
        return True


def _is_local_path(value):
    return (value.startswith("/") or value.startswith("\\\\")
            or bool(re.match(r"^[A-Za-z]:[\\/]", value))
            or value.startswith("file://") or value.startswith("./") or value.startswith("../"))


def _sanitize_remote(value):
    """Remove transport credentials and local absolute paths from portable identity."""
    if _is_local_path(value):
        return None
    if re.match(r"^[^/@:]+@[^:]+:.+", value):
        host, path = value.split(":", 1)
        host = host.rsplit("@", 1)[-1]
        return f"ssh://{host}/{path.lstrip('/')}"
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    # Relative/path-like remotes are machine-local and must not become identity.
    if value.startswith(".") or "/" in value or "\\" in value:
        return None
    return value


def resolve_project_remote(repo, remote_name=None):
    """Inspect only ``repo`` with read-only Git and return a portable sync config."""
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Project repository does not exist")
    def git(*args):
        proc = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=10)
        if proc.returncode:
            raise ValueError("Cannot inspect the supplied project repository")
        return proc.stdout.strip()
    remotes = git("remote").splitlines()
    remotes = [item for item in remotes if item]
    if remote_name is not None:
        if remote_name not in remotes:
            raise ValueError("Requested remote does not exist in the project repository")
        selected = remote_name
    elif "origin" in remotes:
        selected = "origin"
    elif len(remotes) == 1:
        selected = remotes[0]
    elif not remotes:
        selected = None
    else:
        raise ValueError("Project has multiple remotes; choose one explicitly")
    identity = _sanitize_remote(git("remote", "get-url", selected)) if selected else None
    return {"mode": "git-only", "remote_name": selected, "repository": identity,
            "data_ref": "refs/heads/workbench-data", "min_interval_seconds": 300,
            "quiet_seconds": 30}


def agent_instructions():
    return """# Research Workspace project data\n\nThe platform repository supplies the schema, setup tools, and agent instructions. This project repository owns only its portable project identity and project data/configuration. The private SQLite database lives in the service-owned local/host data directory; setup and the scoped service API initialize and migrate it. Never create, copy, merge, or push SQLite database files.\n\nUse this project's configured Git remote and the dedicated `refs/heads/workbench-data` ref for portable Git synchronization. Keep that data ref separate from `main`, `master`, and source branches. A project with multiple remotes requires an explicit choice. GitHub identity and timestamps are attribution metadata, never record keys; project and record identifiers are UUIDs; service revisions and snapshot hashes guard edits. Use only project-scoped CLI/API operations and revision checks. Do not copy application code into this repository.\n"""
