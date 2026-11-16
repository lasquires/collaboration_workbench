# Research authoring implementation plan

> For agentic workers: use superpowers:executing-plans or subagent-driven-development for bounded tasks. Live ownership, progress and acceptance are recorded only in the source SQLite workspace.

**Goal:** Let a human turn a scoped observation or question into a revisioned finding, propose a pinned answer, obtain independent review and resolve or reopen the question using the same records as agents.

**Architecture:** Keep the existing findings/answers transaction handlers as authority. Add direct scoped entity contexts, then a standalone dashboard component with explicit paged related reads, guarded forms and exact transaction recovery. Reuse the existing map, access helper and project navigation; a task-context link carries project and task UUID without changing the map.

**Tech stack:** Existing Python/SQLite API, framework-free browser JavaScript, CommonJS Node fixtures, real loopback HTTP/Edge verification. No new runtime dependency or schema migration is required for the context reads.

**Spec:** Existing collaborative research workspace and standalone Git workspace designs under docs/superpowers/specs; API contract handoff recorded in C01-RESEARCH-AUTHORING-CONTRACT.

## Global constraints

All reads and mutations require explicit project scope. Finding classifications are authored assertions, not independent forensic acceptance. Preserve historical revisions, scope, contradictory proposals and evidence withdrawal history. A withdrawn link is never restored; new reviewed support uses a new finding revision or answer. Answers require 1–100 pinned links, and an answer author cannot independently review their own proposal. A review does not automatically resolve a question. No automatic rebase, submit, review, resolution or evidence withdrawal. Render all authored/source text literally.

## Review focus

- A late response from another project, principal, entity or page must not replace a draft or selected context.
- A lost transaction response must retain its exact actor, expected revision, request ID and body across reload; malformed acknowledgment cannot clear recovery.
- A stale finding/question/support or expired cursor must require explicit reread and reconciliation without dropping authored text or choosing a resolution by default.
- A newer independent disagreement, withdrawn evidence or changed question must remain visible and invalidate the apparent resolution according to backend projections.
- Paged collections must not silently imply completeness. Evidence is pinned to exact artifact versions and locators, not guessed from a hint or current working tree.

## Task 1: Direct research contexts and exact pinned support

Create tools/workspace_research_context.py and tests/test_workspace_research_context.py; register resources in tools/roadmap_research.py and routes in tools/roadmap_server.py. Coordinator owns registration; a bounded backend worker may own only the new module and its tests after a source reservation.

Interfaces:

- GET /v1/findings/{encoded-id}/context?project_id=UUID -> read_v1('finding-context', project, key=id).
- GET /v1/questions/{encoded-id}/context?project_id=UUID -> read_v1('question-context', project, key=id).
- GET /v1/evidence-links/{encoded-id}/context?project_id=UUID -> read_v1('evidence-context', project, key=id). Entity includes original source metadata and current withdrawal state; context.owner is the known finding/answer identity or null, and related.changes points to paged evidence-changes for this exact link.
- GET /v1/answers/{encoded-id}/context?project_id=UUID -> read_v1('answer-context', project, key=id). Answer context also includes context.question as the current question projection from the same snapshot.
- GET /v1/answer-support?project_id=UUID&subject_id=answerID&limit=20&cursor=... pages only the exact referenced evidence-link IDs (at most100), including reused finding links. Rows include link provenance/state and source_sha256/binary_build_id/availability/local_bytes_available. Cursors bind resource, project, answer, limit and snapshot; no hidden project-wide evidence scan.
- No optional filters, cursor or limit on a single-entity context route. Unsupported parameters reject400. Missing/foreign entity rejects404; project authorization remains the HTTP shared-read gate.
- Response: {api_version, project_id, revision, coverage, next_cursor:null, context:{entity_type, entity, related}}.
- entity uses the existing finding/question list projection: current revision, decoded JSON, record_revision and describe() support/resolution state. Do not embed history collections in this response.
- related is a mapping of section name to {resource, params}. Finding: revisions -> finding-revisions subject_id=finding.id; evidence -> evidence-links subject_type=finding_revision subject_id=evidence_origin_id; notices -> research-review-notices with the same revision scope. Question: answers -> answers subject_id=question.id; resolutions -> question-resolutions subject_id=question.id. Answer: support -> answer-support subject_id=answer.id; reviews -> answer-reviews subject_id=answer.id; notices -> research-review-notices subject_type=answer subject_id=answer.id. All descriptor params include project_id. Other historical revision/answer reviews use their exact identities through existing paged APIs.

Steps:

1. Write failing direct-store and real HTTP tests: correct latest row/record guard/scope, no hidden history embedding, foreign same-ID isolation, missing404, unsupported filters400, owner/member project access, read does not advance revision.
2. Implement the single-entity projections using scoped SQL and existing describe helpers; share API envelope/coverage helpers. Preserve existing list projection semantics and imports.
3. Run the context tests plus finding/answer/shared HTTP suites. Independently review the bounded read surface before accepting this task.

## Verified rejected-write protocol

The authoring controller may clear a rejected pending body only when error.data.transaction_rejection exactly matches {project_id,principal_id,request_id,expected_revision,state:'not_applied'} from that frozen global transaction. The store emits this marker only after an actual rollback of an eligible validation/not-found/conflict failure and a committed-log absence check. Existing committed IDs, authentication/unknown failures, record-mode requests and HTTP response failures have no such proof and remain uncertain. Controller reread/reconciliation still requires explicit human action and preserves authored text. No generic HTTP status alone is sufficient.

## Task 2: Research controller and exact recovery

Create docs/workspace-research-session.js and tests/workspace_research_session.cjs for the controller; coordinator creates docs/workspace-research.js for DOM rendering/integration. Export browser global/CommonJS WorkspaceResearchSession from the controller. Interface createSession({api,getContext,onChange,onDirty,storage,requestId}) with state(), load(), open(type,id), readPage(resource,params,cursor), edit(kind,seed), change(field,value), submit(operation), retry(), discard(), refreshContext(), reconcile(operation,acknowledged). getContext returns {projectId,principalId,activePanel}; panel name research. The separate WorkspaceResearch.create(...) renderer consumes the controller and renders #researchWorkspace and returns the controller entry methods needed by the dashboard. API uses injected WorkspaceAccess-aware api and JSON POST headers.

Use existing GET collections/findings/questions/artifact-versions and direct contexts. Related pages use explicit limit20 and exact context descriptors. Keep previous pages separate on snapshot changes; do not append a different revision. Support selected historical finding revision/evidence origin, answer reviews and question resolution history. Artifact/version selectors show ID, source hash/build/availability; direct evidence forms capture version, locator (string or explicit structured locator), method and limitations.

Freeze a one-operation /v1/transactions envelope {project_id, actor:{principal_id}, expected_revision, request_id, operations:[operation]} before POST. Persist its exact serialized body in workbench.research.pending.v1 with a 2 MiB bound; storage failure sends nothing. Create finding/answer operations have explicit immutable IDs. Validate recovered body/project/actor/allowed operation and bounds. Reload reads only. Recovery infers an existing target context from finding_id/question_id/answer_id/evidence_link_id without rewriting the frozen request, so reread after rejection can preserve author text. Never replace an uncertain action with a new ID; acknowledgment must match request_id and service revision identity with changed_records. A known local stale response retains authored draft for explicit reread/reconciliation, not an automatic new transaction. edit captures its base global revision; later readPage/capability reads do not silently replace that base. refreshContext preserves authored fields. reconcile(operation,true) explicitly adopts a newly reviewed operation and current read revision without submitting; the renderer supplies every field choice rather than defaulting conflict choices. Principal/project recovery mismatch blocks writes until original access/context returns.

Operation builders must preserve the backend contract:

- create_finding {id,claim,scope,classification,uncertainty,evidence}; revise_finding {finding_id,expected_record_revision,fields,rationale}; archive_finding/restore_finding {finding_id,expected_record_revision,rationale}.
- set_evidence_state {evidence_link_id,expected_state:'available',state:'retracted'|'superseded',reason,replacement_id?}; supersession requires distinct available support. No revive control.
- create_answer {id,question_id,expected_question_revision,claim,evidence_link_ids,evidence?}; review_answer {answer_id,expected_question_revision,outcome,rationale,supersedes?}. Supersession is an explicit list of the current principal's own review IDs.
- resolve_question {question_id,expected_record_revision,answer_id,rationale}; set_question_state {question_id,expected_record_revision,state:'open'|'investigating'|'withdrawn',rationale}.

Steps:

1. Write failing fixtures for the five review-focus conditions, exact lost-response/reload recovery, no default choices, self-review prevention, immutable versions, evidence withdrawal and stale/expired paging. Test actual state transitions, not a copy of implementation branches.
2. Implement project/entity/principal context generations, one in-flight mutation, scoped drafts and pending-body validation; validate each read envelope and exact receipt.
3. Run Node tests and independent review. Coordinator integrates only after file ownership is released; no overlapping edits or branch switches.

## Task 3: Human workflow and map integration

Coordinator modifies docs/roadmap-dashboard.html, docs/task-graph.html, tools/roadmap_server.py asset allowlist, README.md and view integration fixtures. Add Findings & answers navigation and #researchWorkspace, recovery routing and dirty guard/discard integration. Existing Evidence observation/finding cards and Question/Hint cards gain contextual actions into this panel. Observation seeds preserve exact occurrence/version/namespace/scope/locator; a hint is relevance only and does not become evidence automatically. Map selected-task context links to the same project/task UUID; new finding scope uses that UUID and leaves unrelated scope fields intact.

Renderer: a paged collection/selected detail layout, finding create/revise/history/archive/restore, source support and withdrawal/supersession with rationale, question answers and pinned evidence, explicit independent review (including disagreement and own-review supersession), selected accepted answer resolution/reopen and support-change notices. Show stored/effective question state and review concerns. Known missing review capability or own author disables review, while backend authorization remains final. Preserve text on all rejections; all forms have accessible labels, stable focus, responsive layout and literal text rendering.

Steps:

1. Add failing integration/navigation fixtures before registering new controls and context links. Preserve existing panels and recovery protocols.
2. Wire the standalone component and shared task selection; update help with the distinction between authorship, independent review and forensic acceptance.
3. Run context/finding/answer/shared HTTP tests and all Node suites. Browser fixtures must exercise observation->hint->finding->answer->independent review->resolution, the question-first path, withdrawal reopening, historical versions, stale409, lost response/reload, permissions and narrow-screen overflow. Use independent authenticated principals; do not fabricate acceptance by changing the actor label.
4. Independent code review and a final scope-appropriate regular regression gate precede scoped commit/push. Parent completion has its own acceptance transaction; source authority migration and structured task workflows remain separate.


## Readable research context checkpoint

The accepted human-interface design now presents claims, authored classification,
uncertainty, exact known task/occurrence/area/source scope, review/resolution
concerns and pinned source tuples before a collapsed full original record.
No original field or unknown scope is removed. Active and historical answer
selections remain distinct; bounded review meaning stays visible. Omitted read
metadata is not represented as a value missing from storage.

Actual DOM/session red-green fixtures cover readable concerns/limitations,
historical selection, unknown scope retention, literal source markup and omitted
versus null source fields. Independent review found no fidelity or state defect.
Authenticated phone-width browser checks showed document/body375px within390px,
collapsed raw provenance, readable full citation and working local draft discard.
After a second client revised uncertainty, a stale browser save retained authored
claim/rationale and disabled Save. Explicit latest-context read and five field
choices preserved the remote limitation and draft claim. Reconciliation did not
save; the final explicit save produced record revision3 with all3revisions retained.
The disposable fixture was stopped and viewport restored. Broader common-origin,
conflicting-build/negative-search presentation and source authority separation
remain distinct gates; this checkpoint does not establish native proof.
