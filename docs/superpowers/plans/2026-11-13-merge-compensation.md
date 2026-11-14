# Reviewed merge compensation

Finish the accepted original workspace Slice 5 compensation gate. Preserve exact
`reverse_merge`; add explicit `compensate_merge` when later work prevents exact undo.

1. Add `tools/workspace_merge_compensation.py`, using the caller connection only.
   `preview_merge_compensation(db,project,merge_id)` compares original-before,
   merge-after and current snapshots. Emit stable action keys for merge-changed
   task fields/columns, containment required flags and incident links. Actions
   carry original/after/current presence/value; conflicting later work recommends
   current but every action requires an explicit original/current choice.
2. Expose `RoadmapStore.merge_compensation_preview` and scoped GET
   `/v1/tasks/merge-compensation-preview?project_id=...&merge_id=...`. Preview guards
   all current/original affected tasks and ancestry, related endpoints, claims and
   redirect state. Include record/structure guards for record concurrency mode.
3. `compensate_merge` needs survivor task/record guard, merge_id, preview_guard,
   exact choices, rationale and nonempty source_references. Compile chosen edits
   over current state; preserve fields/new links untouched by the original merge.
   Deactivate that merge's redirect and restore the absorbed task as a separate
   unarchived review task. Pair status/gates require review; never restore passed
   acceptance. Only touched completed scope/claims must be explicitly reopened or
   released. Reject overlapping later merges and final cycles atomically.
4. Schema 10 adds immutable project-scoped merge_compensations with request,
   preview/choices/final snapshot/source references and attribution. Insert audit
   only after store normalization/finalization so final counters match. Public
   audit omits local claims/session/reservation state; full private reviewed event
   remains in local history with a source-context digest. Include the audit in
   portable exchange with backward-compatible old snapshot reads.
5. Record guards cover the full compensation preview scope. Verify an undone
   pair can merge again using a new redirect history row; old merge records and
   compensation audits stay immutable/history-preserving.
6. Tests first: later child text survives restored containment; explicit conflicting
   title choices; stale/missing-choice rejection; active claims and cycles; later
   links/dependencies retained; exact replay; merge again after undo; portable
   audit, scoped HTTP and record-mode guards. Run relevant structure/exchange/live
   suites, full backend suite, UI fixtures, then independent code review.

No source-native behavior is asserted. Synthetic workflow evidence checks these
transaction and history guarantees; UI integration remains a separate parent gate.
