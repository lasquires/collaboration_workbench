# Connect a project to Research Workspace

Research Workspace is installed separately from your project. You need Python
3.11 or newer and Git. The examples use `python`; use your configured Python
interpreter if that command is unavailable. Replace `<app-install>` with the app's
installation directory. No Python dependencies need to be downloaded for the
local launcher.

## Start the app

From any directory, run:

```text
python <app-install>/workspace.py serve
```

The default address is `http://127.0.0.1:8771/`. Local app data is stored under
`%LOCALAPPDATA%/ResearchWorkspace` on Windows, or
`~/.local/share/research-workspace` elsewhere. Use `--home <directory>` for an
explicit data location and `--port <port>` for another loopback port. An existing
project database is not automatically copied or replaced by this command.

## Preview and apply project setup

Use the repository root, not a nested source directory:

```text
python <app-install>/workspace.py setup --repo <project-root>
python <app-install>/workspace.py setup --repo <project-root> --apply
python <app-install>/workspace.py doctor --repo <project-root>
```

Use `--url http://127.0.0.1:<port>` on setup and doctor if the app uses another port.
The first command shows the files that setup proposes without contacting the
service or writing project files. The second registers a project and its local
repository connection through the service, then writes:

- `.research-workspace/project.json`: portable project identity, name and project-specific remote binding;
- `.research-workspace/schema.json`: versioned storage/portable-record contract;
- `.research-workspace/AGENTS.md`: the project-agent integration contract.

Repeated setup reuses the project identity. A clone must retain its existing
manifest to join the same research project. Local repository paths remain in the
app's database and are not written into the portable manifest. Existing integration
instructions are not overwritten if they differ from the generated guide.

Add a reference to `.research-workspace/AGENTS.md` in your project's existing
agent instructions, preserving unrelated content. Review and commit those three
integration files using your normal Git workflow. The app does not commit or push
them automatically.

## Use the workspace safely

Open the app to select your project. Use scoped API/CLI transactions for tasks,
claims, evidence and journals. The compatibility HTTP client is available as
`python <app-install>/tools/roadmap_cli.py --url <service-url> --help`; its `v1-read`
and `v1-transaction` commands support project-scoped work. Actor labels on the
local service are trusted labels, not authentication.

Never edit SQLite directly, copy live WAL files as a backup, merge database files,
or infer verified software behavior from workflow completion or hints. Preserve
source hashes, methods and limitations when reporting evidence.

## Synchronization

Project setup and automatic Git source refresh are implemented. Enable portable
[Git sharing](git-sharing.md) explicitly for each connected project. Optional
[live hosting](self-hosting.md) supports authenticated reconciliation and Git
fallback; its configuration/review APIs are available before the complete browser
controls. Check connection status and resolve conflicts through reviewed previews.
A successful repository connection alone does not establish synchronized research.

Snapshot synchronization does not transfer active claims as shared leases. Local
clients report local-only claims. Use the explicit host claim preview/submit/read
APIs described in [live hosting](self-hosting.md), or connect to the host directly,
for shared coordination. Never infer a host lease from portable task data.
Task journals share their full text, evidence and correction links as immutable
records in schema 9. Read them with the scoped task history/context endpoints;
imported author labels are provenance, not authentication grants.


## Independent edits

Use the advertised [record concurrency](record-concurrency.md) mode when editing
unrelated tasks, questions or documents in tandem. Supply guards for every affected
record and parent scope; keep the exact request ID/payload for uncertain retries.
Global mode remains required for bulk synchronization and unsupported operations.
Never silently refresh a stale guard after 409.


## Reconcile task identity changes

Use the service's merge/reversal previews. When later work prevents exact undo,
use [reviewed compensation](merge-compensation.md): choose original/current for
each proposed change, retain later work deliberately, and inspect the immutable
audit. Never restore an old SQLite file to undo a task edit or merger.

## Task identities when connecting several repositories

Use project UUID plus task UUID for integrations. Labels such as `TODO` or `C01`
may be repeated in independent projects. The app maps imported storage keys
locally; do not rename another project's tasks to avoid a label collision. Those
mappings stay in the app database and are not project configuration or portable
research data. Exchange snapshots retain their original keys and hashes, journal
source records remain unchanged, and merge recovery translates a scoped view.
Public aliases remain attached to their original task UUIDs.

## Publish findings with pinned support

Use `create_finding` through `/v1/transactions` with `claim`, `scope`, authored
`classification`, `uncertainty`, and an `evidence` array. Each source descriptor
contains `artifact_version_id`, exact `locator`, `method`, and `limitations`.
A scope may name project-local task UUID, area, occurrence and source version;
explicit identifier/namespace/scope keys enable deterministic question matching.
Unsourced findings are allowed and remain visibly unsupported. A classification
is an author assertion, not platform-granted forensic acceptance.

Use `revise_finding` with `finding_id`, `expected_record_revision`, `fields` and
`rationale`. Claim/scope/classification/support changes create a new evidence
origin; uncertainty edits are editorial and preserve prior hint decisions. Read
`/v1/finding-revisions?project_id=...&subject_id=<finding-id>` for history and
`/v1/evidence-links?project_id=...&subject_type=finding_revision&subject_id=<evidence-origin-id>`
for support. Both reads are bounded and have snapshot-bound cursors. Supported
finding edits also accept explicit record concurrency with a `finding` guard.

`archive_finding` and `restore_finding` retain history and require the finding
record revision plus rationale. Restoring does not erase stale hint decisions.
`set_evidence_state` requires the source link ID, `expected_state`, new state
(`retracted` or `superseded`) and reason; supersession also names a distinct
available `replacement_id`. This operation uses global concurrency. It records
immutable evidence history and project review notices without reviving old source
links. Reads: `/v1/evidence-changes` and `/v1/research-review-notices`.

Finding publication and imports queue durable local matching; publication alone
does not resolve a question. Use the explicit answer workflow below.

## Propose, review and select an answer

`create_answer` requires `question_id`, `expected_question_revision`, `claim`,
and 1–100 pinned sources. Supply `evidence_link_ids` from available source support
or direct `evidence` descriptors using the same version/locator/method/limitations
shape as findings. A proposal freezes the question requirements and source digest;
propose an alternative instead of editing its prior claim.

An independent project principal with current `contribute` and `review` capability
uses `review_answer` with `answer_id`, `expected_question_revision`, `outcome`
(`accepted` or `rejected`) and `rationale`. Optional `supersedes` names only that
reviewer's earlier decisions on this answer. Contradictory review heads remain
disputed; clocks do not choose a winner. Authentication and current capability
are checked again for exact request replays.

`resolve_question` explicitly selects an `answer_id` and requires the question's
`expected_record_revision` plus `rationale`. It requires fresh independently
accepted support. `set_question_state` reopens or withdraws with `state` equal to
`open`, `investigating` or `withdrawn`; it cannot bypass resolution by selecting
`resolved`. All four answer/question operations support explicit record mode
using the affected `question` guard.

Read `/v1/answers?project_id=...&subject_id=<question-id>`,
`/v1/answer-reviews?project_id=...&subject_id=<answer-id>` and
`/v1/question-resolutions?project_id=...&subject_id=<question-id>`. These lists
have bounded pages and revision/filter-bound cursors. A question read includes
its recorded state, effective state, selection and review concerns.

Source withdrawal, changed supporting finding semantics or changed question
requirements invalidates affected selections while retaining all earlier proposals,
reviews and resolution events. Editorial finding uncertainty changes preserve
acceptance. Capability revocation changes effective trust without rewriting the
shared authored resolution.

Portable Git/live data retains review and resolution history. An imported row is
not evidence that this service authenticated its claimed reviewer, even if that
principal has local membership. Local review receipts stay outside exchange. A
shared resolution remains visible as `recorded_state=resolved` while its effective
state can require local review; a fresh authorized independent review establishes
local acceptance. Shared-service users review directly on their host. This is a
workflow acceptance gate, not automatic verification of a native behavior claim.
