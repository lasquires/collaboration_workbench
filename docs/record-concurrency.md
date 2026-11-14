# Explicit record concurrency

The default `/v1/transactions` envelope uses `expected_revision` and global
compare-and-swap. Legacy `/api` writes always keep that behavior. To edit unrelated
records concurrently, choose explicit record mode and omit the global revision:

```json
{
  "project_id": "<project-uuid>",
  "actor": {"principal_id": "<your-principal-uuid>"},
  "request_id": "<new-request-uuid>",
  "concurrency_mode": "records",
  "expected_records": [
    {"type": "task", "id": "<task-uuid>", "revision": 4}
  ],
  "operations": [
    {"op": "update", "task_id": "<task-uuid>", "fields": {"summary": "New progress"}}
  ]
}
```

Use the actual task UUID and `record_revision` returned by scoped context or task
reads. Operation references can still use project aliases; task guards use stable
UUIDs and never follow redirects implicitly. Each successful transaction still
advances the global revision for read cursors and global-mode clients.

## Required guards

The service derives affected records inside its write transaction and rejects
missing guards. Additional supplied guards are checked too. The capability read
at `/v1/projects/<uuid>/capabilities` advertises `concurrency_modes`,
`record_concurrency_types`, `record_concurrency_operations` and `limits.record_guards`.

| Type | Identity and revision |
| --- | --- |
| `task` | Task UUID and its `record_revision` |
| `task_structure` | Task UUID and its aggregate `structure_revision` |
| `question` | Question ID and its record revision |
| `document` | Document ID and its revision |
| `proposal` | Edit proposal ID and its revision |
| `feedback` | Pinned document-version ID; current actor's feedback revision, or zero if absent |
| `area`, `project` | Scoped area/project ID and its revision |

Text edits need the edited task. Task relationships also need their endpoints.
Moves, child creation, archives and scope edits include affected parent/ancestry
structure guards. Completion includes the task's aggregate scope and its current
children. Merge/reversal includes all touched task and structure guards from the
reviewed scope. Existing operation-specific record, parent, and preview guards
remain required; record mode does not bypass domain rules or acceptance gates.

Task-owned mutable claims, attempts, blockers and relationship changes advance
the task revision, even when its visible title/summary does not change. Acceptance
criteria changes advance its structure scope. Independent append-only journal
entries can coexist without overwriting each other.

Question edits need the question guard. New document versions and edit proposals
need the document guard; accepting a proposal needs both proposal and document.
Feedback guards are always bound to the current principal, so one person's review
cannot overwrite another person's feedback. Read your current vote and
`my_feedback_revision` from `/v1/feedback`. Shared authentication binds this read
to your principal; local trusted clients pass `principal_id` explicitly.

Bulk exchange, indexing/maintenance and operations not listed in capabilities
require global mode. A rejected record-mode operation never silently falls back
to global mode. Do not combine `expected_revision` with record mode, or send
`expected_records` without explicitly choosing record mode.

## Conflicts, retries and authorization

Guards describe the initial state of the whole atomic transaction. A missing or
malformed guard returns 400, a changed guard 409, and a foreign/missing scoped
record 404. A failing operation rolls back the complete transaction. The original
global-mode stale-write behavior remains unchanged.

After 409, reread and explicitly reconcile. After a lost response, retry the exact
same request identity and payload. An already committed request returns its
original result without applying twice, even though its old record guards are now
stale. Reusing its identity with changed content conflicts. Current membership,
session binding and bearer authorization are checked under the transaction lock
before either replay or mutation; revision mode never grants capabilities.

Agents can submit the exact envelope using the existing `v1-transaction` CLI
command and a UTF-8 JSON file. Keep credentials outside project repositories.


Local record and aggregate counters cannot rewind when Git/live imports bring
independent remote counters. A changed local head advances its local guard even
if the imported row reused the same number. Counter-only maintenance is not a
research edit and does not generate repeated Git/live updates. Shared content
merges do not choose winners using revision counters or update timestamps; the
original source snapshots and history remain immutable.
