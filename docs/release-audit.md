# Independent project and release audit (2026-11-16)

## Scope and evidence

This audit checks the standalone application/project boundary, reusable project
setup contract, and focused Git-only/live synchronization contracts. It does not
claim production SSG2 cutover or whole-product completion. Sources reviewed:
`docs/HANDOFF.md`, `docs/release-acceptance.md`,
`docs/project-agent-setup.md`, `docs/git-sharing.md`, `docs/self-hosting.md`,
and the standalone Git workspace and hybrid collaboration specifications.

Code evidence confirms the intended separation: the launcher supports an
independent private app home; project setup writes only `.research-workspace`
integration metadata; project UUID and Git remote bindings are explicit; portable
records exclude local DB/credentials; Git synchronization uses a dedicated data
ref; and live hosting is opt-in with authenticated project scoping. These claims
are directly mapped to `tests/test_standalone_app.py`,
`tests/test_workspace_project_contract.py`, `tests/test_workspace_sync_service.py`,
`tests/test_workspace_git_roundtrip.py`, `tests/test_workspace_live_service.py`,
and `tests/test_workspace_live_client.py`.

The project agent guide is actionable for generic projects: preview/apply setup,
doctor diagnostics, preserve clone identity, keep DB private, use project-scoped
transactions, and distinguish local claims from host leases. Its detailed sync
and evidence caveats avoid overclaiming. Setup of SSG2 itself remains a separate
consumer/cutover acceptance, not demonstrated by a generic contract fixture.

## Focused check

Command, using the configured runtime:

```powershell
& 'C:\Users\drsqu\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -B -X utf8 -m unittest tests.test_standalone_app tests.test_workspace_project_contract tests.test_workspace_sync_service tests.test_workspace_git_roundtrip tests.test_workspace_live_service tests.test_workspace_live_client -q
```

Result: **37 tests passed in 58.789 seconds** with `sandbox_permissions:
require_escalated`. The initial sandboxed attempt was not valid product evidence:
Windows denied disposable loopback sockets (`WinError 10013`) and temporary Git
pushes. That attempt produced 6 errors and 4 failures across the same selection;
all passed when rerun with local fixture permissions.

## Release gates open at the initial audit (superseded below)

- The source-criticism parent still lacks its larger combined conflicting-build,
  shared-origin, mobile and lost-response/recovery browser acceptance; its child
  tests and fixture evidence do not close the parent gate.
- Reconcile all parent acceptance gates against the current branch, including
  the full interactive multi-repository flow and any still-unrun integrated
  research/task collaboration gates. The reported 519-test regular regression
  intentionally excluded two large source-roundtrip cases.
- The actual SSG2 project identity/setup split and production authority migration
  need owner-coordinated backup, exact comparison, recovery and explicit cutover.
  Prior work only migrated/restored private copies and preserved the original
  service. Do not represent that as production cutover.
- App delivery is distinct from production SSG2 authority: a release of the
  standalone workbench can be judged on app contracts, but neither app feature
  completion nor synthetic fixtures switch SSG2's work authority. Conversely,
  the game cutover requires access and acceptance evidence beyond this app audit.

No actionable product defect was established in this bounded audit. The focused
project/mode tests pass when their local socket and Git fixture requirements are
available. This report is an evidence checkpoint, not a full release sign-off.

## Final application review and gate reconciliation

The coordinator subsequently completed the combined real-browser source-criticism
and two-repository onboarding gate. The initial application gaps above are now
superseded by the delivery matrix in `docs/release-acceptance.md`. That browser
runner verifies current/historical/archived assertions, shared recorded origin,
wrong-input run rejection, exact-source entry links, citation withdrawal, visible
limits, bounded searches, 390px layout, guarded drafts, postcommit lost-response
recovery and real server-proven stale reconciliation. It also previews/applies two
new Git projects, refreshes committed evidence, switches projects without evidence
leakage and verifies contributor-only setup rejection.

Further independent read-only review found no actionable issue in the source
UI/controller/backend, the visible observation details, or the final stdin
isolation across all six Git subprocess helpers. The Git transport still passes
the exact supplied input bytes when present; only commands without input receive
closed stdin. A diagnostic reproduction established the Windows inherited-pipe
failure before the fix. The production timeouts were retained, and actual browser
onboarding passed afterward. The coordinator reviewed the worker-authored doctor
change and its focused standalone regression.

All 14 existing Node suites and both separately gated scale cases passed. The
current regular-regression receipt is in the release report. This closes the
scoped **application** implementation/acceptance audit using synthetic projects
and the preserved historical browser evidence. It is not a claim that any
forensic interpretation is true or that real SSG2 production authority moved.
The original project's owner-coordinated backup, exact comparisons, rollback and
explicit cutover remain required before that separate deployment can be accepted.
