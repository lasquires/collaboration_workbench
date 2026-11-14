# Reviewed merge compensation

Exact reversal and compensation are separate operations. Use the existing merge
reversal preview when the affected state still matches the merge's after-state.
If later work makes exact reversal unsafe, compensation offers explicit choices
without restoring an old database snapshot.

## Preview and choose

Read:

```text
GET /v1/tasks/merge-compensation-preview?project_id=<uuid>&merge_id=<uuid>
```

The response includes the current global revision, survivor/absorbed task IDs,
record/structure guards and a fresh `guard_digest`. Each action describes a field,
containment flag or relationship that the original merge changed. It shows
original, after-merge and current presence/value. A conflicting later change
recommends keeping current; recommendations are never submitted automatically.

Copy each action's exact `key` into a `choices` object and choose `original` or
`current` explicitly. Every action needs a choice, including nonconflicting ones.
Fields and new relationships untouched by the original merge remain current.
The preview also shows mandatory effects: restore the absorbed task's separate
identity, unarchive it, and require review of the pair's acceptance.

## Apply through the owning service

Submit a normal scoped transaction with one `compensate_merge` operation:

```json
{
  "op": "compensate_merge",
  "task_id": "<survivor-task-uuid>",
  "expected_record_revision": 7,
  "merge_id": "<merge-uuid>",
  "preview_guard": "<fresh-guard-digest>",
  "choices": {"<exact-action-key>": "current"},
  "rationale": "Explain the reviewed restoration and retained later work",
  "source_references": ["docs/reports/actual-review-evidence.md"]
}
```

Use the actual action keys, task revision, actor and request identity. Include the
preview's global revision in global mode. Explicit record mode instead needs both
record and structure guards for every task in the preview's `guards` map; see
[record concurrency](record-concurrency.md). Existing operation-specific guards
remain required. Independent compensations may share one atomic transaction.

The chosen final graph is checked atomically. Stale preview/context, missing
choices, overlapping later merges, affected active or expired-unreleased claims,
completed/archived affected scope, and cycles prevent unsafe application. Release
or hand off affected claims and reopen completed scope explicitly before a fresh
preview. Choices can retain a later parent/link to resolve a would-be cycle.
`apply_available` describes the basic preview; the final chosen plan still needs
these checks.

Later notes and links are preserved. Compensation does not restore passed
acceptance or imply native correctness. Original merges, losing alternatives,
source references and prior events remain available. The original merge records
its reversing request, and a later new merge uses a new history identity.

## Read and share the audit

```text
GET /v1/merge-compensations?project_id=<uuid>&task_id=<task-uuid>&limit=50
GET /v1/merge-compensations?project_id=<uuid>&compensation_id=<audit-uuid>
```

Lists return bounded metadata summaries. Request a specific audit for its full
preview, explicit choices, rationale, sources and final applied snapshot. Optional
`merge_id` filtering is supported. Cursors bind project, filters and revision;
after a changed snapshot, restart pagination rather than rebasing a cursor.
Shared credentials can read only their project.

Schema 10 stores immutable compensation records keyed by UUID. Request IDs are
transaction identities, not unique audit identities: one request may produce
several records. Exact lost-response replay returns the original result and does
not insert duplicates. A changed payload needs a new request ID.

Portable Git/live exchange retains the audit. Its public snapshots omit local
claims, reservations and session fields while preserving authored values and
historical alternatives. A digest links the original private review context;
full private operation detail remains in local event history. It is provenance,
not an independent verification of a native claim. Invalid audit JSON/project
bindings are rejected before persistence.

Service startup checkpoints a schema-9 database before migration. Original merge
snapshots and applied bundles are retained unchanged, and old bundles without the
new audit table remain readable. Upgrade collaborators to the current app before
sending its new schema. Browser compensation controls are a separate UI gate;
these APIs and project-agent instructions are usable directly.
