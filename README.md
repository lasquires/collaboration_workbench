# Collaboration Workbench

A standalone local research workspace for software reconstruction. Connect Git
repositories, organize scoped tasks and evidence, discuss findings, and preserve
source provenance and research history across projects.

## Run the app

Requires Python 3.11+ and Git. The local application uses Python's standard library;
no Python dependencies need to be downloaded.

```text
python workspace.py serve
```

Open `http://127.0.0.1:8771/`. Application data lives outside project repositories:
`%LOCALAPPDATA%/ResearchWorkspace` on Windows or
`~/.local/share/research-workspace` elsewhere. Use `serve --home <directory>` to
choose another data location. You can launch `workspace.py` by absolute path from
any working directory.

## Connect a repository

With the app running, open **Repositories** to preview and apply a local repository
connection in the UI. Project agents can use the equivalent commands:

```text
python workspace.py setup --repo <repository-root>
python workspace.py setup --repo <repository-root> --apply
python workspace.py doctor --repo <repository-root>
```

Setup first previews the portable project manifest and agent instructions. Apply
registers the project through the local service and writes those integration
files. Repeated setup retains the project identity without changing workspace
state. Different repositories can have separate projects; clones retain the same
portable manifest to join an existing project.

See [the project-agent setup guide](docs/project-agent-setup.md) for instructions
that another project's agents can follow. App code, local database files and clone
paths do not belong in connected project repositories.

## Development status

This is an actively developed prototype, not a complete release. Independent
startup, project registration, tasks, evidence indexing, matching hints, documents,
question editing, and guarded core merge/reversal APIs are implemented.

Automatic Git source refresh, shared research synchronization, optional live-service
request authentication and transport, complete merge conflict compensation, and final
end-to-end/scale acceptance are still being completed. Doctor reports pending
synchronization explicitly. A successful connection is not evidence that research
has synchronized, and workflow completion never establishes forensic correctness.

The relevant source history was preserved during extraction from the SSG2
prototype. See [extraction provenance](EXTRACTION.md) and the standalone design and
implementation plan under `docs/superpowers/`. No game assets, native binaries,
credentials or live project databases are included in this repository.

The [hybrid collaboration design](docs/superpowers/specs/2026-11-13-hybrid-collaboration.md)
covers optional free self-hosting, Git-only fallback, batched synchronization and
offline reconciliation. Authentication and read-only Git snapshot modules have
isolated tests; their presence alone does not enable remote sharing or continuous
synchronization.
