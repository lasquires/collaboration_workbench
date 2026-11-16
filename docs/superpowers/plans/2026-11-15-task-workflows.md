# Structured task workflows implementation plan

> For agentic workers: use bounded subagent-driven-development with exclusive ownership recorded in the source task service. Workers must not spawn workers.

**Goal:** Make task scope, attempts, blockers, independent validation, completion and merge recovery usable from the selected map task without losing uncertain writes or bypassing backend acceptance.

**Architecture:** Keep the existing structure and compensation handlers authoritative. Add a standalone guarded task session and renderer, then connect the map inspector to them. Separate typed local/service claims from live-host lease controls. No schema change.

**Tech stack:** Existing framework-free JavaScript, Python/SQLite API, Node fixtures and authenticated loopback browser fixtures.

## Files and ownership

- docs/workspace-task-session.js: scoped reads, drafts, bounded exact durable transaction recovery and explicit reconciliation.
- tests/workspace_task_session.cjs: actual protocol/race/reload/storage/receipt tests.
- docs/workspace-task-workflows.js: operation builders, task-context sections and explicit guarded forms.
- tests/workspace_task_workflows.cjs: builders and DOM interaction fixtures.
- docs/task-graph.html and tools/roadmap_server.py: coordinator integration only after current research reservations are reconciled; no overlapping worker writes.

## Controller contract

Export WorkspaceTaskSession.createSession({api,getContext,onChange,onDirty,storage,requestId}). Context is projectId,principalId,taskId,activePanel; panel name task-workflows. Expose state/load/readPage/edit/change/submit/retry/discard/refreshContext/reconcile. Exact GET /v1/tasks/UUID/context with project scope and capabilities. A task redirect must be shown and explicitly adopted, not silently substituted into a draft target.

Draft captures global revision plus task record/structure guards. Later collection reads cannot advance an authored base. Persist one-operation /v1/transactions envelope before any POST under workbench.task.pending.v1, maximum2MiB. Restore constructor-time, never autoPOST. Creation is not handled here; selected-task mutation kinds only. Validate exact envelope keys and IDs, allowed operation, selected task UUID, record guard and rationale. Reject unknown/malformed pending bodies without deleting them. Startup recovery must expose original project/principal/task so map can route back before reads.

Receipt requires original request_id, matching project if supplied, matching installation revision prefix and changed task receipt for the original target. Exact retry uses frozen serialized bytes. Only exact five-field transaction_rejection matching project/principal/request/base/state=not_applied proves rejection; other errors stay uncertain. Cleanup failure retains the frozen entry and blocks new writes. Known rejection retains author text, requires fresh exact selected context and explicit reconcile(operation,true), no automatic retry or guard replacement. Reject target switching during reconciliation.

Paged attempts/validations/blockers/claims use explicit20-record pages and exact task filters; only blockers accept state=open. Keep one current page per resource, reject mixed snapshot/filter/cursor responses. Bound late results by project/principal/task/panel generation. All reads remain read-only, including previews.

## Task forms

Write failing fixtures before implementation. Use exact current record guard and explicit rationale for each operation. Attempts capture boundary/outcome/evidence and explicit resolution. Blockers capture typed target and resolution. Validation requires review capability, independent identity, inputs/outputs and pinned version evidence; completion is a separate action and the backend parent gate remains final. Scope/move/archive/reopen forms preserve parent/ancestor structure guards and never use graph pan/drag as a structural edit.

Claims show local/service scope, lease expiry and retained reservations. Explicit claim/heartbeat/handoff/release only; no automatic renewal. Preserve manage_claims authorization and distinguish these from remote-host leases.

## Merge recovery

Read exact merge/reversal/compensation previews with bounded visible sections and immutable guards. Require explicit field/metadata/action choices, source references and rationale. No default conflict resolution. Safe reversal uses its current preview; intervening edits route to explicit compensation, preserving later work. Durable pending recovery covers these mutations too.

## Integration and verification

- [ ] Controller protocol Node tests and independent review.
- [ ] Renderer builders/DOM fixtures for typed operations and no premature completion.
- [ ] Integrate selected-task workflow panel, help/filter/context sections and recovery routing; preserve map navigation/draft guards.
- [ ] Authenticated browser flows: failed attempt/blocker resolution, independent validation, parent completion, move/archive/reopen, claim conflict, merge/reverse/compensate, lost response/reload and stale guards.
- [ ] Phone-width layout/literal source text/focus checks, task-relevant Python suites, all Node suites and final regular regression.
- [ ] Independent review and parent-specific acceptance before scoped publication. Source migration/cutover remains a separate backed-up gate.

Contract evidence: roadmap_structure.apply_operation/guard/parent_guards/read, roadmap_projection context, workspace_merge_compensation and roadmap_server workspace_read/shared_write. Read-only taskC01-TASK-UI-CONTRACT records exact handler/test audit. Native parity is outside this workflow gate.
