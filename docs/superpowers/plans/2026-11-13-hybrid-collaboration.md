# Hybrid collaboration implementation plan

> Use superpowers:executing-plans. Track claims and progress only through the live
> SQLite task service. This plan specifies deliverables, not live task status.

**Goal:** Support app-owned live database coordination and batched Git-only
synchronization without losing offline or concurrent research progress.

**Design:** `../specs/2026-11-13-hybrid-collaboration.md`, extending the earlier
standalone and collaborative-workspace specifications.

## 1. Finish local connection and source refresh integration

Use `workspace.py` and the repository panel for preview-pinned setup and project
selection. Integrate `tools/workspace_git_watch.py` through service-owned background
jobs. Store immutable repository snapshots and visible cursors/errors. Run Git
inspection outside write transactions, then publish with revision/identity guards.
Preserve pinned old sources and treat bounded/incomplete scans explicitly.

Tests must cover first commit, clean commits, branch switches, repeated dirty
edits, Unicode/deleted/renamed sources, unavailable bytes, restart, no-op scans and
project isolation. Browser verification must connect two repositories and show
source changes without losing an unsaved task/research draft.

## 2. Authenticate optional live project service

Integrate `tools/workspace_auth.py` through a versioned, backed-up migration.
Implement private owner bootstrap, bearer request verification, project-filtered
reads, actor binding and scoped mutations in `tools/roadmap_server.py`. Reject
unscoped legacy access in shared mode. Add explicit listen/TLS/proxy configuration
and fail closed when authentication/transport configuration is incomplete.

Provide client token configuration and browser invite/session controls without
putting secrets in Git. Recheck invitation issuer authority at redemption and
support revocation. Tests must exercise HTTP isolation, spoofed actor rejection,
wrong project tokens, expiry, double redemption, rollback, unauthorized setup and
TLS startup. Do not expose a real host while these checks remain incomplete.

## 3. Complete service-mediated exchange application

Route bundle registration/import through the owning store transaction. Validate
identities and pinned support, preserve local capabilities/leases, and enforce
review/acceptance gates on composed final state. Keep all alternatives/history.
Resolve task/source cache synchronization without a second writer.

Test compatible merges, deleted/absent fields, concurrent same-field edits,
immutable identity conflicts, imported privilege escalation, validation references,
retries and restart. Integrate reviewable import/export controls in the app.

## 4. Batch portable changes through app-owned Git state

Add a sync module and CLI/UI actions using a dedicated workspace-data ref and
private app-owned Git metadata storage. Leave project branch/index untouched.
Persist ancestor/tip and pending changes. Apply a quiet-period debounce and default
five-minute minimum automatic network interval; implement Sync now and no-op
suppression. Fetch/reconcile before an ordinary push; handle rejected pushes by
fetching/reconciling again without force updates.

Use two clones/clients plus a temporary bare remote to prove concurrent additions
survive, conflicting fields are retained for choices, an outdated push cannot
replace progress, staged source edits stay unchanged, and failed/retried jobs
remain durable. Do not use timestamps to pick task-state winners.

## 5. Reconcile offline work when a host returns

Maintain a durable client change outbox with stable operation IDs/base revisions.
Select live/Git-only behavior per project. On service return, authenticate and
reconcile queued changes before resuming live leases. Keep local claim intent
separate from host-acknowledged ownership. Publish batched host checkpoints for
Git-only participants and recovery.

Test host stop/restart with two clients editing meanwhile, duplicate deliveries,
uncertain requests, contradictory progress and stale validations. Verify no lost
history, false acceptance or silent global claim ownership.

## 6. Complete original and deployment acceptance

Finish merge compensation/application/reversal UI, record CAS, accepted answers,
source retraction, discussion/help, project separation and SSG2 backed-up migration.
Run original recovery/scale gates and hybrid scenarios together. Document real
self-host setup without mandatory paid services and repeat onboarding using only
published project-agent instructions. Verify Git-only startup requires no host.

Completion requires evidence for all of these gates and the original feature
plans; a public repository or standalone module test suite is not full delivery.


### Immutable journal implementation detail

Keep task journal content in project-scoped immutable records, keyed by original
event UUID and referencing the stable task UUID. Preserve the complete source
event, exact details JSON, installation UUID and content digest. Backfill from
local events on a backed-up schema upgrade without granting membership from a
display label. Existing local events remain untouched. Exchange the records using
the same immutable-identity collision rules as evidence revisions. Integrate them
into paginated task history, de-duplicating existing local event IDs. Corrections
remain append-only and may reference an imported entry on the same task.

Acceptance exercises independent-store text/evidence/correction round trips,
original provenance, schema-8 backfill without event/membership changes, malformed
payload rejection without writes, stable pagination and existing Git/live flows.
Old immutable snapshots remain unchanged; missing journal rows mean unknown
historical content, never fabricated progress. This does not transfer leases.


### Explicit host-lease command integration

Keep live claims outside portable data. Owner-operated local controls read the
host, prepare one frozen revision-guarded claim/heartbeat/release/handoff command,
and submit only its stored review identity. Bind pending replay to the original
host installation and authenticated principal. Store uncertain requests privately
and block host replacement; never acquire or renew a lease from the background
snapshot worker. A transaction acknowledgment is distinct from a current lease.
Verify competing workers/path reservations, stale revisions, exact replay after
commit-response loss and restart, offline behavior, principal/host replacement,
capability enforcement and guest proxy denial using actual authenticated HTTP.
