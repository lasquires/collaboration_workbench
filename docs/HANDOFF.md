# Continue Collaboration Workbench

Updated 2026-11-16. The app is not finished. This document is sufficient to resume without the original conversation.

## Get the right checkout

Public repository: https://github.com/lasquires/collaboration_workbench

Stable code baseline: `3c590b9e5fe58caff52f6c88602a210ef6143c3d` on main. Main also receives this handoff documentation. The unfinished source-criticism checkpoint is published separately as `codex/source-criticism-handoff`. Continue from that branch, preserving its changes. The local branch was named `codex/standalone`; a different local name does not mean a different application.

Open the **research-workspace** directory as the next chat's workspace. Suggested prompt: **Finish this app. Follow AGENTS.md and docs/HANDOFF.md. Continue the source-criticism checkpoint.**

## Product requirements

A separate app connects multiple Git repositories, indexes their committed code/assets and displays updates. Shared tasks, research and evidence belong to each connected project, with instructions agents can follow to initialize that project's schema/contract. They do not belong in the app's development repository.

Support optional free self-hosted live collaboration and Git-only fallback. Git publishing is batched (quiet period and approximately five-minute synchronization), not one commit per edit. Portable records and journal history merge semantically, retaining concurrent alternatives and explicit resolution; never merge binary SQLite databases or use millisecond timestamps as conflict authority. The UI should use FamilySearch/Puzzilla-inspired tree/graph navigation and inspectors, adapted to software research.

## Verified baseline

Implemented: standalone launcher/private data home, repository registration/source watching with pinned committed bytes, authenticated live service, Git data-reference synchronization, portable tasks/research/history, conflict/recovery handling, graph/radial/outline navigation, inspectors/discussion, task reservations/handoffs/acceptance, findings/questions/answers/reviews/citation withdrawal.

`docs/release-acceptance.md` maps requirements to evidence and remaining gates. It is an evidence snapshot, not the live task board. Latest broad baseline: 503 Python tests passed with one optional skip (two large source-roundtrip cases gated separately). Subsequent targeted and Node checks covered later changes. Do not describe the broad baseline as a run of the unfinished checkpoint.

An authenticated browser research loop was exercised end-to-end: finding from exact observation, question, pinned answer, independent authenticated review, resolution, citation withdrawal, stale answer and investigating question with history retained. Mobile and explicit stale-draft reconciliation were checked. These checks do not cover the new source-criticism UI.

Original SSG2 schema-4 data was backed up with SQLite-aware tooling, migrated only in private copies to app schema 17, restored and tested through the normal launcher. Original rows/history were compared and the original authority was left unchanged. No production cutover has occurred.

## Unfinished checkpoint: source criticism

Read `docs/superpowers/plans/2026-11-16-source-criticism.md` and the source-criticism section of `docs/superpowers/specs/2026-11-12-collaborative-research-workspace-design.md`.

Backend/controller and pure builders exist; the actual UI wiring and browser acceptance remain unfinished. No new schema/table/version was introduced.

Changed files:

- `tools/workspace_source_context.py`: exact-version source context, derivation history, scoped published observations and negative searches; bounded pagination and strict revision-bound cursors; supplied-bundle validation.
- `tools/workspace_findings.py`: typed derivation scope/support validation on create/revise/import.
- `tools/roadmap_research.py`, `tools/roadmap_server.py`: source-context routes and scoped resources.
- `docs/workspace-research-session.js`: source selection, exact child binding, scoped reads, negative-search submission/recovery. Preserve unknown future scope metadata.
- `docs/workspace-research.js`: exported derivationSeed and buildNegativeSearchOperation plus typed finding validation. These are builders, not finished forms.
- `tests/test_workspace_source_context.py`, `tests/test_workspace_source_context_http.py`, `tests/workspace_research_session.cjs`, `tests/workspace_research_views.cjs`: backend/HTTP/controller/builder coverage.

Typed derivation is an ordinary finding scope: `artifact_version_id` is the child; `derivation` contains `format: workbench-derived-source/v1`, `parent_version_id`, and optional published `index_run_id`. Both exact parent and child need citations with user-entered locator/method/limits. Shared run means recorded common origin; matching bytes alone proves neither independence nor dependence. History, archive and support availability remain visible. Unknown format metadata stays ordinary metadata.

GET `/v1/artifact-versions/{id}/context?project_id=...` exposes source context. Related descriptors identify resources `source-derivations`, `observations`, `negative-searches` with exact project/subject parameters. Source derivation rows carry historical/current/archive flags and support_counts. Source versions can have null/absent binary_build_id. Read the implementation/tests for exact envelopes rather than inventing fields.

### Next concrete work

1. Wire source opening from catalog, existing Evidence comparison, observations and findings. Extend dashboard URL selection to accept source (currently finding/question/answer/evidence).
2. Add source-context actions and forms: record scoped negative search with explicit complete/partial coverage, query, method, searched range and outcome; record derivation with parent-version and optional published-run picker. Never invent methods/limits or infer proof of absence.
3. Render derivation/common-origin history with current/historical/archive/support states and parent/child/finding links. Reuse the existing Evidence browser and two-item comparison.
4. Show conflicting builds side-by-side with readable exact hashes, locators, uncertainty and conclusion limits; do not select a winner automatically.
5. Exercise actual authenticated browser flows with two conflicting builds, two reports sharing an explicit run, bounded negative search, limitations, mobile, draft navigation and lost-response/stale recovery.
6. Obtain independent review and run relevant regression gates. Only then close source-criticism tasks.
7. Finish project/authority separation and final parent release audit. Existing source authority must not silently become app development authority. Verify per-project setup instructions and both Git-only/live modes against the release matrix.

## Validation and running

From this repository, use Python 3 and Node. App default: `python workspace.py serve`; private home defaults under LOCALAPPDATA/ResearchWorkspace and loopback port 8771. Follow README for initialization/authentication. Never commit private homes or fixture credentials.

Focused checkpoint command:

```text
python -B -X utf8 -m unittest tests.test_workspace_source_context_http tests.test_workspace_source_context tests.test_workspace_findings tests.test_workspace_research_context -q
node tests/workspace_research_session.cjs
node tests/workspace_research_views.cjs
```

Session and renderer suites passed on 2026-11-16 after preserving unknown future derivation metadata. At handoff, the focused Python set passed 37 tests in 13.391 seconds; all 12 `tests/workspace_*.cjs` suites passed. For all Node fixtures, run each `tests/workspace_*.cjs` and set WORKBENCH_TEST_PYTHON to the Python executable for HTTP integration fixtures. Consult release-acceptance for larger separately gated tests. A passing focused set does not close missing UI/browser gates.

## Same-machine coordination appendix

App: `C:/Users/lukes/Documents/ChatGPT/research-workspace`.
Legacy source: `C:/Users/lukes/Documents/ChatGPT/ssg2`.
Python: `C:/Users/lukes/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`.

Existing task authority remains SSG2 `analysis/roadmap.sqlite3`, schema 4, loopback `http://127.0.0.1:8769`; use its service/API or `tools/roadmap_cli.py`, never direct DB writes. Read its BOARD workflow before claiming. App schema is 17. Do not confuse API export schema_version with DB schema. Source service was left running; check availability rather than starting a competing instance.

Resume task context for C01-SOURCE-CRITICISM, C01-SOURCE-CONTEXT-BACKEND and C01-SOURCE-SESSION. These remain unfinished. Claims are released for handoff; reread context and claim explicit paths before writes. C01-PROJECT-SEPARATION and C01-STANDALONE contain remaining cutover requirements. On 409 reread and reconcile explicitly; parent completion needs its own gate.

Private acceptance evidence was in system Temp folders `workbench-legacy-acceptance-p5xulh3p`, `workbench-full-research-3dzbqnmt`, and `workbench-sections-regular-sm6tjzpp`; it is optional local evidence, not a dependency or something to publish. Fixture services were stopped; original source service was retained. Do not print tokens from fixture metadata.

Sibling-app writes required scoped sandbox escalation in the old SSG2 chat. Opening the app itself as workspace avoids that friction and the unrelated game-restoration instructions. Preserve unrelated SSG2 changes. There is no need to reconstruct the old conversation or repeat already verified baseline features.
