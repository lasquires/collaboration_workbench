# Standalone delivery acceptance evidence

This report maps the agreed application behavior to concrete verification. It is
not the live task board or a claim of native software parity. The accepted scope
is the research workspace design, standalone Git workspace design, and hybrid
collaboration design under `docs/superpowers/specs/`.

## Application and project boundaries

| Requirement | Evidence | Practical limit |
| --- | --- | --- |
| Separate application, private local data, launch outside the game repository | `tests/test_standalone_app.py`: launch from unrelated working directory, independent home, two repository setup and idempotency | Original game service remains the live game-work authority; copying its data for acceptance does not cut it over |
| Project-owned manifest, schema contract and agent setup | `tests/test_workspace_project_contract.py`; `workspace.py setup` and `doctor`; `docs/project-agent-setup.md` | Multiple Git remotes require an explicit selection; no credentials or SQLite files belong in project Git |
| Observe committed Git changes and retain exact source bytes | `tests/test_workspace_git_watch.py`, `test_workspace_git_capture.py`, `test_workspace_git_transport.py` | Dirty source files are distinct from committed evidence; supported extraction coverage is explicit, not full semantic indexing |
| Shared tasks, research, source manifests and immutable history | `tests/test_workspace_portable.py`, `test_workspace_findings.py`, `test_workspace_answers.py`, `test_workspace_link_sync.py` | Imported review labels do not confer local authenticated acceptance; leases and local filesystem bindings are not portable |
| Git-only synchronization with batching and no source branch/index mutation | `tests/test_workspace_sync_service.py`, `test_workspace_git_transport.py`, `test_workspace_sync.py` | Explicit enablement/export selection; 5-minute network interval and quiet period; no force push or clock-based conflict winner |
| Optional self-hosted live service and offline reconciliation | `tests/test_workspace_shared_http.py`, `test_workspace_live_client.py`, `test_workspace_live_service.py`, `test_workspace_live_review_replay.py`, `test_workspace_live_claims.py` | Hosting is opt-in; remote use requires TLS and scoped bearer credentials; no paid hosting is required or provisioned automatically |

## Human workflow evidence

The map supplies labeled branches, radial exploration, outline and selected task
context. `tests/task_graph_view.cjs`, `workspace_layout.cjs` and
`workspace_navigation.cjs` verify navigation, bounded focus history, responsive
layout and draft isolation. Earlier authenticated browser fixtures exercised
map selection and task-context navigation. This is workflow inspiration from
FamilySearch/Puzzilla, not a claim of a pixel-identical replica.

Published task workflow browser checks cover failed-attempt recording and
resolution, blockers, move/archive/reopen, explicit claim/renew/release,
independent validation followed by separate completion, merge/reversal and
compensation after a later edit. Exact lost-response task retry and stale-context
reconciliation preserve authored fields. Selected task sections include overview,
subtasks, knowledge, questions, discussion and history with local action filtering.
The phone-width task fixture measured document/body 375px inside a 390px viewport,
with 306px fields. Drafts survived section/filter navigation. Full-page links now
block unsaved drafts; explicit discard restores ordinary navigation.

Research browser checks cover an indexed observation becoming an authored
finding with its exact occurrence/version/scope/locator/method/limitations;
explainable hints opening the question without selecting support automatically;
an explicitly pinned answer; separate authenticated independent review and
resolution; and support withdrawal reopening a prior conclusion. An authored
classification remains an assertion and self-review stays disabled. The
question-first path and source-support histories retain original identities.

The final research lost-response fixture committed a finding before returning
503. Reload restored the pending request and sent no POST automatically. An
explicit retry sent the same request ID and byte-identical body: 2 POSTs, 1 finding.
The original project and selected task scope survived. This tests browser wiring
in addition to the protocol fixtures in `workspace_research_session.cjs`.

## Actual legacy checkpoint, upgrade and restore

A deliberate stopped-service checkpoint used the original SQLite-aware backup
CLI. The original service restarted without a schema or authority change. Only a
private copy of the legacy store was opened by standalone production migration.
All original columns and rows in every pre-existing table except migration
metadata were compared before and after: no values changed. This includes 114 tasks,
656 events, aliases, task raw/original JSON, research source blobs and hashes,
claims and reservations. The immutable legacy backup SHA-256 was
`a691f17dbb01d1ddf9252edbf9a9e7450dc64ed78c480b84ca4f5c36e20e8efc`.

The production pre-upgrade checkpoint was restored into a second private app
home and upgraded there. Its complete snapshot equaled the first upgraded copy.
The normal `workspace.py serve` command then served the restored copy. A synthetic
agent/API journal entry appeared in browser Discussion; a browser-authored
attempt appeared through the API. After stopping and restarting that service,
the one attempt remained. The original service retained its checkpoint revision
and task count and did not contain the synthetic task. Both acceptance services
were stopped; the original game service remains available.

Private diagnostic artifacts are retained locally under the uniquely named
`workbench-legacy-acceptance-p5xulh3p` temporary directory. They contain private
project data and are intentionally absent from Git. This proves copy migration
and restore, not a production authority switch or workbench/game project split.

## Verification scope and remaining release evidence

The regular Python regression at the published navigation fix ran 503 tests in
363.514 seconds with zero failures/errors and one optional fixture skip. The two
large source-roundtrip tests are a separate scale gate, not silently included in
that count. Previously recorded scale runs cover 20,000 committed tokens and an
explicit 50,000 observation/10,000 task Git-plus-two-live-client fixture. All 13 then
existing JavaScript suites passed. New joined acceptance suites must be reported
with their own exact results before their corresponding gate is accepted.

The joined `tests/workspace_task_http.cjs` fixture uses a real authenticated
service. It checks an explicit parent-completion rejection without a revision
change, required-child and independent parent gates, protected path overlap with
retained rejected draft fields, authorized handoff, and submitted release with
empty reservations afterward. Run with `WORKBENCH_TEST_PYTHON` pointing to the
Python runtime when the platform default `python`/`python3` is unavailable.

`tests/test_workspace_git_roundtrip.py` uses independent installation IDs, explicit
initial bootstrap, actual cloned source checkouts, and production Git sync. It
reviews a concurrent same-field task edit, preserves losing alternatives and full
journal text, converges both ways, and proves repeated sync creates neither a
commit nor another import. Source branch tips, raw index bytes, staged/unstaged
status and working file bytes remain unchanged. A separate case in the same
module exercises the actual registered `WorkspaceSyncService`: it pushes the
workspace-data ref while preserving the registered source repository's full
HEAD/status/index/worktree state and remote source-branch tip, then verifies
repeat-tick stability. Both cases passed together (2 tests, 15.606 seconds). The standalone scenario passed;
all 14 JavaScript suites, including joined HTTP acceptance, also passed.
The focused Git transport/sync/portable/recovery/roundtrip Python gate ran
39 tests in 42.830 seconds with zero failures/errors.

The final release review still needs explicit reconciliation of all parent acceptance
gates. The source project authority split and production cutover remain separate,
backed-up work. Research source criticism presentation (conflicting builds,
common-source derivatives and scoped negative searches) must be audited against
the design's human-interface requirements; backend storage alone cannot prove
those screens are usable. No full-product completion is inferred from this report.


## Source-criticism checkpoint review (2026-11-16)

The handoff branch now exposes exact-source context from the version catalog,
citations and Evidence observations, with explicit parent-version/published-run
selection for ordinary typed derivation findings. History shows current/historical
assertions, archived findings, available/retracted/superseded citation counts and
page-bounded recorded common-origin groups. Existing observation comparison adds
source hashes and locators with a scope-difference warning.

Red-green DOM/model fixtures cover these controls, literal authored text, exact
child identity, blank user-authored methods/limits, explicit search coverage and
no writes while selecting sources/runs. All 12 `workspace_*.cjs` suites passed.
An independent read-only UI review found no important issues. A separate backend
integration review found no important defects; its 14 source-context/HTTP tests
passed. The guarded source-session fixtures passed again, including frozen retry,
no automatic POST after recovery, subject binding and explicit reconciliation.

An authenticated disposable browser fixture saved/read a partial-coverage negative
search and a derivation with explicit citations to both exact versions. The view
retained uncertainty, source hashes and authorship. The larger combined conflicting
build/shared-origin-run/mobile/recovery acceptance fixture remains open. These
bounded child gates do not complete the source-criticism parent or final release.

The subsequent regular Python regression on the source-criticism checkpoint ran
519 tests in 393.656 seconds: zero failures/errors and one optional OpenSSL-dependent
skip. The two large `SourceRoundtripTests` were explicitly excluded and remain a
separate scale gate. This is a current regression result, not completion of the
remaining human-interface or authority-cutover acceptance requirements.

## Delivery reconciliation (2026-11-16 continuation)

This section supersedes the open **application** gates in the earlier snapshots.
The original SSG2 production authority cutover remains external to this delivery.
The checkout was cloned directly from `codex/source-criticism-handoff` at
`07327f8807c326f0f0645ed0701f0ecf674d4082`; prior commits were preserved.

| Application acceptance gate | Current evidence | Result and boundary |
| --- | --- | --- |
| Source opening from observation, catalog, finding and citation | `tests/source_criticism_browser.cjs` against the production authenticated HTTP handler | Exact source identities and task-scoped deep links survive each entry path; browsing never writes |
| Conflicting builds and cautious comparison | Same browser fixture, two published source builds | Both distinct hashes, builds, locators and competing values appear; no winner is selected |
| Explicit derivation and common origin | Two browser-authored reports with two reviewed citations each | Explicit parent and published run retained; wrong-input run rejected without a finding; shared run labeled as recorded common origin, with page-bounded grouping |
| Retained interpretation and citation history | Browser revision, archive and citation withdrawal followed by parent history read | Current/historical/archive/support states and parent/child/finding links retained |
| Bounded negative search and visible limits | Browser partial-coverage search and exact API readback | Literal query, source, method, range and outcome retained; another source has no matching search; unsuccessful search is explicitly not proof of absence |
| Mobile, draft guards and recovery | Actual 390px browser, server commit followed by synthetic lost reply, reload and explicit retry; separate real stale rejection | No document overflow; authored fields survive navigation; reload sends no POST; retry bytes and request ID identical with one saved record; stale draft needs exact-context read, explicit adoption and a separate Save |
| Independent projects and reusable onboarding | Browser owner previews/applies two unconfigured committed Git repositories, refreshes each and switches projects | Separate manifest UUIDs and isolated indexed evidence; schema/agent contract written; no DB in either project; contributor-only setup denied |
| Git-only batching, merge alternatives and unchanged source repositories | `test_workspace_sync_service`, `test_workspace_git_roundtrip`, transport/sync/portable/recovery suites | Actual Git data-ref publishing and concurrent reconciliation retain history and alternatives; source branch/index/worktree retained; five-minute interval and quiet-period contracts covered |
| Optional free self-hosted live mode | Shared HTTP, live service/client, live claims/review/replay suites | Authenticated project scope, opt-in hosting, offline reconciliation and explicit conflict review; no paid host provisioned |
| Task, research, document, discussion and recovery parents | Regular Python suite, all 14 existing Node suites, joined authenticated task HTTP fixture and prior browser evidence above | Current APIs/controllers and parent-specific task acceptance gates pass; workflow acceptance does not imply native provenance |
| Scale | `WORKBENCH_SCALE_GATE=1 python -B -X utf8 -m unittest tests.test_workspace_large_sync.SourceRoundtripTests -v` | Both 50,000 observations/10,000 tasks and 20,000 committed-token fixtures passed together: 2 tests, 672.286s; real Git plus two authenticated live clients |

The combined browser gate found two real issues: scoped observations hid their
identifier/value/method in collapsed provenance, and Git subprocesses inherited
the service controller's blocked stdin pipe on Windows. Observation details are
now readable beside citations. Git helpers use closed stdin for commands without
input; transport commands that author objects retain their exact explicit bytes.
The production timeout and test acceptance expectations were not relaxed.

The parent release audit also found stale `doctor` guidance claiming Git sync was
unimplemented. Doctor now reports actual scoped Git state, safe error/retry details,
source/evidence status and a bounded local-byte availability summary, with actions
for disabled, pending, failed and review-required states. Private bindings and
cache details are omitted. The focused standalone module passed all 10 tests;
the Git provenance/standalone/watch/capture/roundtrip check passed 39 tests in
36.598s before the final all-helper stdin isolation pass.

All 14 existing Node suites passed after integration. The combined authenticated
browser gate passed with headless Microsoft Edge and the configured Python
runtime. Independent read-only reviews covered the source UI/controller/backend,
observation readability fix, standalone/project contracts and Git/live modes.
See `docs/release-audit.md` and `docs/verification.md` for review boundaries and
reproduction commands. The final regular-regression receipt is recorded below.

Final code regression: **519 Python tests ran in 406.999 seconds, zero failures
or errors, one optional OpenSSL-dependent skip**. This run followed all final
doctor and Git stdin changes and excluded only `SourceRoundtripTests`, whose two
separate successful scale results are recorded above. The final Node pass covered
all 14 existing suites; the additional real-browser suite passed separately.

### Production project authority remains a separate gate

No legacy DB, private evidence, credentials or bearer tokens were copied into
this repository. Application development used an ignored app-only local tracker;
all acceptance stores were disposable. Original SSG2 files, its unrelated edits
and its existing task authority were preserved. Historical copy-migration and
restore evidence above remains valid only within its recorded scope. A real
SSG2 deployment still requires access to authoritative data, supported backup,
exact comparison, rollback evidence, owner coordination and an explicit audited
cutover. Synthetic application acceptance neither performs nor authorizes that
production switch.
