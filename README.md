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

Setup first previews the portable project manifest, schema contract and agent instructions. Apply
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

The project map offers labeled branches and a radial overview, with a task outline
and selected-task context beside the chart. Keyboard selection, bounded focus
history and mobile browsing retain draft guards; view gestures do not write task
relationships or acceptance. Map and research panels retain the selected project.

Repository refresh pins committed source bytes and publishes bounded Unicode
identifier-shaped indexes with explicit coverage. Dirty working copies remain
separate from committed evidence. Optional live-service bearer access,
TLS startup, project-scoped collaborator credentials and browser sign-in are
implemented; see [self-hosting](docs/self-hosting.md). Opt-in batched
[Git sharing and field conflict review](docs/git-sharing.md) are implemented.
Live/offline reconnect and explicit host-claim APIs are implemented. Reviewed live
data commands retain private replay receipts across lost replies and restart.
[Explicit record concurrency](docs/record-concurrency.md) lets supported unrelated
edits proceed with scoped revision guards. [Reviewed merge compensation](docs/merge-compensation.md) is implemented through
the scoped API. Project-scoped task imports preserve repeated labels across
independent repositories through local storage mappings. Authored finding
revisions and evidence withdrawal APIs preserve semantic lineage and prior hint
decisions. Evidence-pinned answers support independent review, explicit question
resolution and automatic review notices when support changes. Explicit journal
sharing review can publish redacted disclosures or omission notices while keeping
original notes and hashes unchanged locally. Reviewed task-prose disclosures through
the scoped API and dashboard preserve private originals during compatible peer
merges. Task and journal approvals recover exact pending requests after reload. Project migration, collaboration UI integration and final end-to-end/scale
acceptance remain open. Large portable bundles use content-addressed chunks in Git
and authenticated staged HTTP transfers; each piece is at most 4 MiB, with a
256 MiB aggregate limit. Private live state verifies cached documents on restart.
The default 20,000-token and extended 50,000-observation/10,000-task fixtures
have passed Git and two live clients with exact record/history checks. Doctor reports pending
synchronization explicitly. A successful connection is not evidence that research
has synchronized, and workflow completion never establishes forensic correctness.

The relevant source history was preserved during extraction from the SSG2
prototype. See [extraction provenance](EXTRACTION.md) and the standalone design and
implementation plan under `docs/superpowers/`. No game assets, native binaries,
credentials or live project databases are included in this repository.

The [hybrid collaboration design](docs/superpowers/specs/2026-11-13-hybrid-collaboration.md)
covers optional free self-hosting, Git-only fallback, batched synchronization and
offline reconciliation. Shared hosting is explicitly enabled; the default remains local. Source snapshots,
portable research synchronization and forensic acceptance have separate status.
