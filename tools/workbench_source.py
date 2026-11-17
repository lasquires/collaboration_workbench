"""Register a local project source and return its authenticated version ID."""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workspace


def register(repo, url, task, root_id, artifact_id, relative_path, kind, build):
    repo = Path(repo).resolve()
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Source must be a repository-relative path without parent traversal")
    source = (repo / path).resolve()
    if not source.is_relative_to(repo) or not source.is_file():
        raise ValueError("Source must be an existing file inside the repository")
    with source.open("rb") as stream:
        body = stream.read(64 * 1024 * 1024 + 1)
    if len(body) > 64 * 1024 * 1024:
        raise ValueError("Source exceeds the Workbench 64 MiB snapshot limit")
    digest = hashlib.sha256(body).hexdigest()
    project = workspace.load_manifest(repo)["project_id"]
    url = workspace.local_url(url)
    # Read every page: absence from the first page does not mean unregistered.
    artifacts, cursor = [], None
    while True:
        query = {"project_id": project}
        if cursor:
            query["cursor"] = cursor
        page = workspace.request(url, "/v1/artifacts?" + urlencode(query))
        artifacts.extend(page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    relative_path = path.as_posix()
    existing = next((item for item in artifacts if item["id"] == artifact_id), None)
    same_source = next((item for item in artifacts if item["root_id"] == root_id
                        and item["relative_path"] == relative_path), None)
    if existing and (existing["root_id"], existing["relative_path"], existing["kind"]) != (root_id, relative_path, kind):
        raise ValueError("Artifact ID already names a different source or kind")
    if same_source:
        if same_source["kind"] != kind:
            raise ValueError("Existing source has a different kind")
        artifact_id = same_source["id"]
    ops = []
    if not existing and not same_source:
        ops.append(dict(op="register_artifact", id=artifact_id, root_id=root_id,
                        relative_path=relative_path, kind=kind))
    ops.append(dict(op="register_artifact_version", artifact_id=artifact_id,
                    expected_sha256=digest, binary_build_id=build))
    ops.append(dict(op="journal", task_id=task, kind="observation",
                    text="Local source registration: " + json.dumps({
                        "artifact_id": artifact_id, "path": relative_path,
                        "sha256": digest, "binary_build_id": build,
                        "boundary": "Authenticated local bytes; no behavior or parity claim"})))
    receipt = workspace.transaction(url, project, ops)
    version = next(item["id"] for item in receipt["changed_records"]
                   if item["type"] == "artifact_version")
    return dict(artifact_id=artifact_id, artifact_version_id=version, sha256=digest,
                request_id=receipt["request_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task")
    parser.add_argument("path")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--url", default=workspace.DEFAULT_URL)
    for field in ("root-id", "artifact-id", "kind", "build"):
        parser.add_argument("--" + field, required=True)
    args = parser.parse_args()
    print(json.dumps(register(args.repo, args.url, args.task, args.root_id,
                              args.artifact_id, args.path, args.kind, args.build), indent=2))


if __name__ == "__main__":
    main()
