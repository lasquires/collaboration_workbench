# Explicit record concurrency

Extend the accepted collaborative-workspace design and Slice 5 concurrency gate.
The default and legacy APIs retain global compare-and-swap. Record mode requires
an explicit `concurrency_mode: records`, `expected_records: [{type,id,revision}]`,
and no global `expected_revision`. Global mode cannot accept record guards.

1. Add `tools/workspace_concurrency.py` as a pure policy/SQL-check module using
   the caller connection. Validate envelopes, supported type/id/revision guards,
   and derive required task/aggregate and supported research/document records.
   Task IDs in guards are stable UUIDs; task references in operations retain the
   existing aliases. Missing guards are 400; stale guards 409; foreign scope 404.
2. Integrate in `tools/roadmap_store.py` under BEGIN IMMEDIATE, after authorization
   and exact request replay checks, before mutation. Keep the original envelope
   in the request digest. Expose supported types/operations through capabilities
   in `tools/roadmap_projection.py`. Uncovered maintenance/bulk operations still
   require explicit global mode; never silently fall back or rebase.
3. Task text edits guard the task. Structure edits also derive all affected
   ancestry aggregate structure revisions; merge/reversal includes their full
   preview scope. Preserve operation-specific rationale/preview guards. Ensure
   mutable task-owned state/links advance task revisions and acceptance changes
   advance own structure scope so aggregate checks detect changed criteria.
4. Cover question revision, document versions/proposals and principal-scoped
   feedback where existing domain revision checks apply. Immutable append-only
   content stays append-only. No database migration or new writer is required.
5. Add failing tests first for independent/same-task edits, missing/stale/foreign
   guards, ancestry/merge races, mixed rollback and exact replay. Verify actor
   authorization under the transaction and shared HTTP/legacy behavior. Run
   task-relevant suites, then the full backend suite and existing UI fixtures.

Record mode is an atomic precondition check, not automatic conflict resolution.
The global revision still advances after every successful transaction and remains
available for read cursors and global-mode clients.


Local revision preservation also covers ordinary global writes and imported rows.
Capture guarded heads/owned task state before mutation and enforce monotonic local
record/aggregate revisions after composition. Remote counters cannot validate an
old local read. Portable synchronization projects counter-only differences onto
the accepted logical head; it does not merge those counters as domain conflicts.
Expose the current principal's feedback revision through scoped feedback reads,
with principal-bound pagination and shared-auth actor binding.
