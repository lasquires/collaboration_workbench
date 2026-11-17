# Repeated Workbench database-busy diagnosis

## Scope and result

This is a bounded source/live-API diagnosis, not a production database inspection.
The production SQLite file was not opened directly, no production write was sent,
and no service was restarted. The available evidence establishes why a lock held
for more than the configured timeout becomes the repeated 503, and identifies
two writer paths that can hold or compete for that lock. It does **not** identify
the process or transaction holding the live database lock, so this is not a
confirmed root-cause attribution.

## Exact 503 path and a disposable reproduction

`collaboration_workbench/tools/roadmap_store.py:154-168` opens an independent
SQLite connection with `timeout=5`, sets `PRAGMA busy_timeout=5000`, and maps
SQLite `locked`/`busy` operational errors to HTTP-compatible
`StoreError(503, "Task database busy; retry after reconciliation")`.
`process_jobs()` reaches `BEGIN IMMEDIATE` at lines 445-449; normal task writes
reach it at lines 626-642. These are competing connections to the same database,
not a nested-transaction self-deadlock within one connection.

I ran a disposable fixture based on
`tests/test_workspace_repository_service.py::RepositoryServiceTests.setUp`.
On its temporary database, a second SQLite connection held `BEGIN IMMEDIATE`
for 6.5 seconds while `RoadmapStore.process_jobs(project, limit=1)` attempted
its own write. Observed result: `StoreError 503 Task database busy; retry after
reconciliation` after **5.534 seconds**. The first call was deliberately blocked
on a second writer, then failed at the documented busy-timeout boundary. This
reproduces the status and timing path; it does not establish that the live lock
is held for the same reason or duration. The unrelated repository-refresh unit
test passed: 1 test, 2.163 seconds.

## Live read-only observations

The sandboxed loopback request was denied by Windows socket policy; retrying the
same read-only GET with the required local-service escalation succeeded. The
`GET /v1/repositories?project_id=474f44a5-90ec-4a0b-864b-c56af207a790` response
reported global DB revision `f8418912-3444-4e5b-a188-a76f8adca0a4:2654`, source
revision `1267`, and source state `incomplete`. Its latest snapshot was timestamped
08:16:48 on Nov 17 and reported 233 changed paths (183 omitted); the listed paths
included volatile `.local/edge-particle-lab` Edge profile, cache, and Crashpad
files. Three further read-only GETs, spaced about three seconds apart, all showed
the same DB/source revisions and latest-update timestamp. The reload log contained
80 lines matching `Repository refresh retry: Task database busy` and was last
written at 08:18:06. This is evidence of repeated lock failures and an actively
changing source-scan target, but neither the GET nor the log exposes the current
lock owner. The `.local` churn is a writer-pressure lead, not proof that it alone
causes SQLite contention.

## Relevant write-lock boundaries

- Repository scans do Git snapshotting and pinned commit capture before acquiring
  SQLite's writer lock (`workspace_repository_service.py:36-70`). Publication
  then takes `BEGIN IMMEDIATE` at line 72. While that lock is held it inserts the
  snapshot, publishes captured source artifacts when needed (`workspace_source_index.py:83-`),
  updates repository state, emits a workspace event, increments revision, writes
  the transaction receipt, and commits (`workspace_repository_service.py:83-114`).
  A changing repository can therefore repeatedly re-enter a writer transaction;
  the server worker catches the busy error and retries every scan loop at
  `roadmap_server.py:807-824`.
- A normal task transaction takes `BEGIN IMMEDIATE` at
  `roadmap_store.py:642`. It retains the write transaction through operation
  processing and the full `_snapshot(db)` at lines 867-875. `_snapshot` reads all
  task rows and every event (`roadmap_store.py:182-217`) before the commit. This
  is a plausible duration amplifier as history grows, but no timing measurement
  here establishes it as the live lock holder.
- The matcher worker calls `process_jobs` at one-second intervals
  (`roadmap_server.py:789-804`) and that function takes an immediate transaction
  even when its result contains no new or stale hints (`roadmap_store.py:445-460`).
  This creates periodic writer participation; its actual lock occupancy was not
  measured in this diagnosis.

## Separate outcomes

`409` is not evidence of SQLite lock contention. A normal transaction returns
revision-conflict 409 when the expected global revision differs after it gets
the writer lock (`roadmap_store.py:686-689`). Repository refresh has a separate
source-revision compare-and-swap 409 if another refresh published while its Git
scan ran (`workspace_repository_service.py:74-76`). Expired task-claim/lease
bookkeeping is also application state, not SQLite lock ownership. A lease may
expire while a request is delayed, but it cannot itself produce the SQLite
`locked`/`busy` exception that is translated to 503.

## Narrowed blocker / next useful measurement

The missing fact is the live writer's connection and transaction duration. A
definitive attribution needs bounded instrumentation at `BEGIN IMMEDIATE` and
commit/rollback, tagged with worker/operation, or an equivalent supported
SQLite lock diagnostic. Do not infer owner from expired claims or 409s. One
separate operational lead is the repository's captured volatile `.local` paths;
an isolated ignore-policy change can measure whether source revisions stop
advancing, but would not by itself prove or repair a long-held SQLite lock.
