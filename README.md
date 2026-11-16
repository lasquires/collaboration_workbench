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

The selected-task workflow panel provides typed attempts/blockers, independent
validation and separate completion, guarded scope/move/archive/reopen actions,
and explicit task-claim renewal/release. Merge, reversal and compensation forms
require fresh previews and explicit conflict choices; later edits are preserved
through reviewed compensation. Exact pending task requests survive reload.
Authenticated browser flows cover these core actions, exact lost-response retries,
reload recovery, stale-context reconciliation and narrow-screen task browsing.
Unsaved drafts guard links and page unload. Final integration gates remain open; see the [task workflow plan](docs/superpowers/plans/2026-11-15-task-workflows.md).

Repository refresh pins committed source bytes and publishes bounded Unicode
identifier-shaped indexes with explicit coverage. Dirty working copies remain
separate from committed evidence. Optional live-service bearer access,
TLS startup, project-scoped collaborator credentials and browser sign-in are
implemented; see [self-hosting](docs/self-hosting.md). Opt-in batched
[Git sharing and field conflict review](docs/git-sharing.md) are implemented.
Live/offline reconnect and explicit host-claim APIs and dashboard controls are
implemented. Paged conflict reviews keep full records in private service caches;
compact browser handles recover exact commands across lost replies and restart.
Historical confirmations are labeled separately from current synchronization or
lease ownership. See the [dashboard live workflow](docs/self-hosting.md#use-the-dashboard-live-controls).
Direct project-scoped finding, question, answer and evidence-link contexts expose current
revisions and paged history descriptors. Exact answer-support pages include reused
finding citations with pinned source metadata; withdrawn support stays visible.
Verified rolled-back global writes can return a request-bound `transaction_rejection`
marker; authentication, unknown or postcommit response failures do not establish
that an edit was not applied. The Findings & answers panel supports paged source selection, finding revisions,
pinned answer proposals, independent review, explicit resolution and evidence
withdrawal. Observation and question cards open that workspace; map task links
carry validated task scope. Frozen pending writes survive reload and require exact
retry confirmation. Authoring integration remains under verification for the
remaining observation/task-link, recovery and mobile browser paths; see the
[authoring plan](docs/superpowers/plans/2026-11-15-research-authoring.md).
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

Selected task workflows include Overview, Subtasks, Knowledge, Questions,
Discussion and History. Sections use explicit bounded reads without advancing
an authored draft's base revision. Knowledge and questions include active task
merge lineage while preserving original task attribution. Exact research links
open the selected finding or question; authored classifications remain assertions,
not forensic acceptance. Discussion shows the retained task journal and audit
history. The action filter is local and never submits or changes a draft.

Research details present the authored claim, unresolved limitations, exact known
scope, current review concerns and pinned source citation as readable fields.
Complete original records remain available under **Full record and provenance**.
Historical answer selections are labeled separately from active selections.
A field omitted by a bounded read is distinguished from an explicitly unrecorded
value; opening the evidence context provides the full pinned citation.
