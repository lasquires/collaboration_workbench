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

## Committed source refresh and citations

Connected repository refresh now captures immutable committed Git blobs and
publishes a bounded `source-token` index. It reads Git objects directly with
replacement refs and lazy object fetching disabled; it does not run project code
or use dirty working-tree bytes as committed evidence. Git must support the
`--no-lazy-fetch` option (the tested environment uses Git 2.46).

Repository reads expose distinct `source_state`, `evidence_state`, `evidence_head`
and `evidence_manifest_id` fields. `evidence_state` is current, partial, failed or
pending. Source refresh, evidence coverage, shared synchronization and forensic
acceptance are separate states. A failed capture retains the previous good
manifest instead of relabeling old evidence as the new commit.

Read `/v1/source-manifests?project_id=...` for bounded summaries, then add
`manifest_id=...` for that commit's file/version mappings and coverage. A citation
can pin commit ID, original Git blob ID, artifact version and content SHA-256. Old
private bytes remain available after later commits or deletions. Raw blobs stay in
the app database and are omitted from portable Git bundles; peers without bytes
can inspect provenance and the explicit local-availability fields. Imported
manifest consistency does not independently prove commit membership or native
behavior on the receiving machine.

A project's manifest UUID defines its main source namespace. Clones retain it;
independent repositories normally get separate project manifests. Same bytes at
the same logical source path reuse version identities and, within the same
extractor configuration, observation identities across commits.
Only generated source profiles with no authored creator coalesce local first-seen
`created_at` differences; original incoming timestamps remain in immutable source
snapshots. Source content, provenance and authored creator differences remain
conflicts. Unrelated commits therefore do not recreate an unchanged token's hint
or erase its dismissal.

For matching, use namespace `source-token`, the manifest file's logical `path` as
`scope_key`, and the exact case-sensitive `identifier`. These are Unicode
identifier-shaped spans, including comments and literals, not parsed symbols or
native meanings. UTF-8 text is scanned; other file bytes are pinned without
interpretation. Exact byte offsets are authoritative, with line/column convenience
locators. The extractor configuration records its Unicode version.

Default capture bounds are 500 file entries, 8 MiB per file, 32 MiB total bytes,
20,000 token spans and 25 seconds; tree metadata also has an 8 MiB read cap.
Exclusions and bounded count/range summaries describe incomplete coverage. Source
bytes remain pinned when a token or logical scope exceeds the research API's
2,000-character index limit, but that lexical omission is explicit. Symlink and
submodule targets are not followed. Dirty and untracked files remain visible in
source-change diagnostics and require explicit manual source-version registration
if they should become research evidence before a commit.

Logical source paths reversibly escape reserved bytes. File details expose
`display_path` and, when needed, `git_path_hex` containing the exact original Git
filename bytes. Non-UTF-8 names use `display_path_encoding=escaped-bytes`; do not
interpret the display representation as an OS pathname or silently replace its
bytes. Use the logical `path` for index scope and the hex form for exact Git
provenance. No source field implies automatic forensic acceptance.

Source capture and transfer have separate limits. Git stores small bundles in the
legacy single-object form and large bundles as a manifest plus SHA-256-addressed
chunks. Live clients opt into `X-Workspace-Chunked: v1`; uploads use
`/v1/transfer-begin`, `/v1/transfer-chunk` and `/v1/transfer-finish`. A large response
returns a `workbench-chunked-response` manifest; retrieve its pieces through
`/v1/transfer-download` and release confirmed staging with `/v1/transfer-release`.
The Python client supports this through `request(..., chunked=True)`.

Pieces are at most 4 MiB; canonical payloads are bounded to 256 MiB. The existing
8 MiB HTTP body bound still applies to every individual request. A transfer binds
its UUID, target, manifest, project and authenticated principal. The host checks
piece lengths and SHA-256 hashes, the ordered payload digest, and the decoded
project before dispatching through the original current-authentication and
revision gates. A partial upload never becomes project data. Retry the same
frozen request after an uncertain reply; a 409 requires explicit reconciliation.
Only portable import/snapshot operations have the larger domain transaction
limit; ordinary mutations retain their existing bound.

Staging is private and excluded from portable records. It expires after 24 hours,
with at most eight jobs per principal, 64 host jobs and 1 GiB of cached piece data.
Full staging fails explicitly; release old transfers or wait for expiry. Local
live state keeps large immutable bundles and previews in hash-verified private
files outside connected repositories, preserving the exact pending request body
across restart. Credentials, raw local database files and cache paths are never
part of the project bundle. The default 20,000-token synthetic committed-source
fixture and an explicit 50,000-observation/10,000-task fixture pass Git and two
authenticated live clients with exact observation, occurrence, task-body, journal
and retained-history comparisons. Run the extended gate with
`WORKBENCH_SCALE_GATE=1` and
`python -B -m unittest tests.test_workspace_large_sync.SourceRoundtripTests`.
These transfer checks do not establish whole-product delivery or native parity.
