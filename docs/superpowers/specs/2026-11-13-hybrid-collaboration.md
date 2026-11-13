# Hybrid live and Git collaboration

## Confirmed product behavior

The user wants a separate application that can connect multiple repositories,
show their updates, and synchronize shared tasks/research as well as source
indexes. Projects receive small setup instructions that their own agents can
follow. The application repository is public at
`https://github.com/lasquires/collaboration_workbench`.

The user additionally chose optional live sharing when it requires no paid
subscription, with Git-only collaboration always available. A project operator
turns live hosting on. Without an available host, independent clients retain local
work and synchronize portable workspace records through Git. This extends and,
where necessary, supersedes the earlier local-only delivery boundary.

## Application-owned database coordination

Agents using one running application already share its transaction boundary.
The app coordinates their writes; this case does not require database-file merge.
Independent application instances need synchronization at the record/event layer.
SQLite files are never merged, and a shared service's SQLite file stays on the
host's local disk behind its API.

In live mode, one authenticated project service owns the current project state,
claims and review gates. Clients use scoped requests. Hosting can use a computer
the operator controls with no mandatory paid platform dependency. The operator
must keep it online and provide secure connectivity for remote participants.
The application does not automatically purchase hosting, expose network ports,
change firewall settings or assume that any provider's free tier is permanent.

In Git-only mode, each client owns a local SQLite projection. Git transports
portable text records and immutable history. Leases, authentication credentials,
local paths and delivery queues are not portable research state. The UI must
identify coordination scope so a local/offline claim is not presented as an
acknowledged shared exclusive lease.

## Identity, history and conflicts

Use UUIDs for record/change identities and explicit base revisions/ancestor
snapshots for edits. GitHub identity, when available, and timestamps are useful
attribution. A name is not a credential, timestamps are not a unique key, and
uniqueness does not resolve concurrent edits to an existing task. Local labels
must not be represented as verified GitHub identities.

Independent additions and edits to different fields combine automatically.
Immutable evidence, attempts, comments and losing alternatives remain available.
Conflicting edits to the same field require explicit choices; neither arrival
time nor a client clock silently selects a winner. Reapply acceptance and
structural invariants to merged state. No imported completion, useful hint or
review label establishes verified native behavior.

## Balanced Git synchronization

Persist edits locally immediately. Debounce portable exports after an idle period;
use a minimum automatic network-sync interval of several minutes (initial default:
five minutes), plus a visible Sync now action. If nothing relevant changed, do not
create commits or push. Pause/retry failed jobs with bounded backoff and visible
status. Background synchronization must not alter a user's staged source changes
or switch their working branch.

Maintain app-owned Git metadata state outside the project working tree, using a
dedicated workspace-data ref in the selected repository. Fetch the current remote
tip before reconciliation. Push with ordinary ancestry/expected-tip checks. If
someone else pushed first, fetch again and reconcile the new changes; never force
push over their progress. Preserve unresolvable alternatives and surface them in
the app instead of losing edits to Git conflict markers.

## Reconnecting an offline client

Keep unacknowledged local changes durable with stable change/request IDs. On live
service return, authenticate, fetch current project state, reconcile queued local
changes against their base revisions, and acknowledge accepted changes atomically.
Retrying an uncertain submission must retain the same identity and payload.
Resume live claims only after the service accepts ownership; do not convert a
Git-exported local claim into a live lease automatically.

The live host also publishes batched portable checkpoints so Git-only clients can
participate and recovery is not tied to the host machine. A Git import is validated
through the same domain transaction/gate rules as other writes.

## Access and deployment boundary

Remote access is opt-in and requires bearer authentication, project isolation,
expiring/revocable credentials and one-time invitations. Store credential hashes
in the database; keep actual client secrets only in private local runtime state.
Never put them in a repository, portable manifest, URL query or domain audit body.
Owner bootstrap is an explicit local administrative operation, not a public API.
An invitation must not grant capabilities beyond its issuer's current authority.

Keep the local mode usable without remote authentication or an external account.
Remote transport must use HTTPS or a deliberately configured trusted TLS proxy;
default startup remains loopback-only. Legacy unscoped APIs must not bypass shared
project authorization. GitHub sign-in may provide identity attribution later,
but an entered GitHub name alone never authenticates a participant.

## Acceptance

Verify two agents sharing one app; two independent clients making offline edits;
concurrent pushes to a bare Git remote; preservation of distinct additions and
same-field conflicts; no source-index/branch mutation; batched/no-op/retry behavior;
two authenticated clients using one host; project access denial and expired/revoked
invites; host-offline editing and host-return reconciliation; restart and backup
recovery; and a real in-app multi-repository flow. Combine these with the original
research, document, structural, migration and scale gates before claiming done.
