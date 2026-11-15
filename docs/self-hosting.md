# Optional live project hosting

The default app stays on loopback. Git-only operation does not require a live
host, a subscription, or a hosted database. A live host runs the same application
and owns its SQLite file; collaborators use its scoped HTTPS API.

## Prepare a host

1. Run `python workspace.py serve`, connect the project repositories, and note
   each project UUID from its `.research-workspace/project.json` file.
2. Stop that service. Run `python workspace.py host-init` to create an owner
   credential in the private app-data directory. The command prints the file
   location and expiry, never the credential itself.
3. Create a collaborator credential for each person or agent:

```text
python workspace.py host-add-member --project <project-uuid> --label "Collaborator"
```

The default capability is `contribute`. Use repeated `--capability` arguments to
choose an explicit set from `contribute`, `review`, `manage_claims`, and
`manage_projects`. Labels are attribution, not verified GitHub identities.
Each credential is scoped to one project; it cannot read the host filesystem.
Share only that collaborator's private credential file through your chosen
private channel. Keep `owner.token` on the host and keep every credential out of Git.

All these commands accept `--home <private-app-data-directory>`. Credential
administration requires the service to be stopped and holds its exclusive lock.
Existing credential files are never overwritten. To revoke a credential:

```text
python workspace.py host-revoke --credential-id <credential-id>
```

## Start explicitly with TLS

Provide a certificate and private key for your service hostname, then run:

```text
python workspace.py serve --shared-mode --host 0.0.0.0 --public-host workbench.example:8771 --tls-cert <certificate.pem> --tls-key <private-key.pem>
```

Use your actual hostname and port. Shared startup refuses missing TLS material
or an absent/expired owner credential. The app does not configure routing,
firewalls, certificates, or third-party hosting. Your existing computer can host
it without a mandatory paid service.

Open the service's HTTPS address. The browser offers a session-only credential
sign-in and an explicit reload. Sign-out clears the tab's stored credential.
For agents, use a private token file rather than putting a token in a command:

```text
python tools/roadmap_cli.py --url https://workbench.example:8771 --token-file <private-credential-file> v1-read projects
```

The CLI verifies TLS certificates and refuses authenticated remote HTTP and
redirects. `RESEARCH_WORKSPACE_TOKEN_FILE` is the equivalent environment setting.
Project mutations must use the credential's principal ID, returned by that
project's capabilities endpoint, and the current revision. Owner-only repository
setup uses an in-process credential-bound service adapter.

## Connect a local app to the host

The backend supports a private, project-scoped live connection. Configure it through
`POST /v1/live-configure` with `project_id`, `enabled: true`, the host `url`, and a
`token_file` path on the local app computer. Keep that file outside every connected
repository and the application repository. The response omits the credential path
and secret. HTTPS is required except for loopback development hosts.

Review `POST /v1/live-join-preview`, then submit its exact review descriptor and
explicit conflict choices to `POST /v1/live-join`. A changed local revision or host
snapshot requires a fresh review. Do not manufacture a replacement review payload.
The service worker then exchanges portable records automatically. Requests whose
responses were lost retain their original identities and are replayed after restart.

`GET /v1/live-status?project_id=<uuid>` reports the connection state and preferred
transport. `POST /v1/live-sync` requests reconciliation. Conflicts use
`POST /v1/live-review` and `/v1/live-resolve`, with `side` set to `host` or `local`;
resolve requires the exact returned review plus explicit field choices. In shared
mode, configuring these filesystem-backed connections requires the local service
owner. A guest credential cannot use the owner's upstream credentials.

While the live connection is available, automatic Git sharing reports
`live-standby`. When the host becomes unavailable, enabled Git sharing resumes its
normal quiet period and network interval. Reconnecting reconciles the queued local
records with the host. Disabling live access retains pending operations for recovery.
An explicit Git Sync now remains available.

## Current boundaries

Authenticated live reconciliation, restart recovery, and automatic Git fallback
have integration coverage using independent stores and a real stopped/restarted
HTTP host. The browser controls for configuring and reviewing live connections
are still being completed.

Local-client claim projections remain **local-only**. Snapshot exchange does not
create an acknowledged host lease. The explicit host claim controls below provide
shared coordination separately. Portable event metadata preserves attribution and
payload digests. Schema 9 also shares immutable task-journal text, evidence, and
correction links through the same portable exchange.
Enabling a listener or completing synchronization never verifies research claims.


## Coordinate claims through a local app

A configured local app can operate the host's claims with the received project
credential. These controls are owner-only on the local app service; shared guests
cannot proxy through its owner's private upstream credential.

1. `POST /v1/live-claims-read` with `project_id` and `task_id` reads the current
   host claim list. Use its returned cursor for additional pages. A failed/offline
   read does not establish availability or ownership.
2. `POST /v1/live-claims-preview` with `project_id` and `operation` prepares a
   concrete command against the host's current service and task revisions. An
   operation names `op`, `task_id`, and `rationale`. Supported operations are
   `claim_task`, `heartbeat_claim`, `release_claim`, and `handoff_claim`.
   Claim creation can include `reserved_paths` and `lease_seconds`. Handoff
   requires the host's claim-management capability and a host principal ID.
3. `POST /v1/live-claims-submit` with `project_id` and the returned `review_id`
   submits the stored command. It accepts an identity, not an editable payload.
   A stale revision requires a fresh preview; it is never silently rebased.

An acknowledged transaction proves the host processed that command. Read the host
claim list again to inspect its current expiry/release state: an acknowledgment is
not a permanent lease. Renew before expiry with an explicit heartbeat operation.
Host rules prevent competing claims and overlapping reserved paths, including
unreleased expired claims that need a handoff or release.

A lost response leaves a private pending command. Retry the same review ID after
restart; the exact original request is replayed without duplicating the claim.
Pending claim state appears under `host_claims` in live status. Disabling the
connection retains it, and replacing the host is blocked until it is resolved.
Retries require the original authenticated principal and host installation.
Credentials and lease state never enter Git exchange, and these operations do not
alter local-only claims. The background data worker never silently acquires,
renews, or retries a host lease.


## Recover reviewed live data commands

Exact reviewed joins and host/local conflict resolutions can be retried after a
lost local-service response or restart. The client binds each command to its
exact review and field choices, authenticated host principal and host installation.
A pending retry resumes its original durable transaction request; it does not
mint a second write. Completed replay returns `reviewed_command` with its digest,
kind and `state: acknowledged`, alongside the current connection status. Current
local contribution and authenticated host access are checked again before replay.

Host acknowledgment can coexist with a remaining local conflict or offline pull.
It confirms the reviewed host command, not completion of all data reconciliation
and never ownership of a live lease. Malformed or mismatched remote transaction
success responses remain uncertain and preserve the original request for retry.
The latest256 reviewed-command receipts stay in private app state and never enter
Git exchange. Older replay fails closed under the existing base/operation guards;
changed choices require a fresh explicit review. Background data synchronization
may finish an already prepared command but never prepares new review choices or
acquires a host claim.


## Use the dashboard live controls

Open the dashboard, choose the connected project, then select **Live collaboration**.
In a trusted local session, the app owner can enable live synchronization, enter
the host URL and the path to the received private credential file, and save the
connection. In authenticated shared mode, these controls require the host owner.
The browser receives the credential path you enter, never the credential contents.
Leave the path blank on later saves to reuse the stored credential. Disabling the
connection preserves queued changes and command receipts.

Connection status names the selected transport and current service state. An
`offline` or `join-required` report can select Git fallback, but this does not mean
Git sharing is enabled or that a push has succeeded. Configure Git sharing separately
under Repositories. Refresh status to inspect the latest background worker report.

Start a join, host-conflict or local-conflict review. Host conflict choices label
HOST current data versus THIS APP queued data; join/local choices label THIS APP
versus HOST. No conflict choice is preselected. Save each page before advancing;
there are at most 20 conflict fields per page. Long values have bounded previews
and explicit full-value slice readers. Immutable history must keep the local value.
After reviewing every conflict, acknowledge the saved choices and prepare the
command. Preparation checks the current host contribution; it never rebases a
stale review. Submit the prepared command explicitly.

If the response is lost, reload and use **Retry exact saved action**. Reload only
reads state; it never submits the command. A server-pending command can also be
confirmed explicitly with its existing handle. HTTP errors alone do not establish
that submission was not applied. An uncertain command cannot be discarded or
replaced with a different host. A verified rejection requires a fresh review.

The service keeps full reviews in private bounded caches. Browser recovery stores
only project/review IDs, command handles and field-choice modes, never record
bundles or token contents. The latest 256 terminal UI confirmations are retained
privately after a review is discarded. Confirming a retained handle can return a
historical receipt without contacting the host; the dashboard labels its time and
states that current access, synchronization and leases were not checked. Corrupt
recovery state fails closed instead of silently replacing an uncertain action.

Under Host task leases, enter the host task identity, rationale and relevant lease
fields. Reserved paths are relative paths, one per line, for a new claim. Preview
shows the concrete stored action; submit that reviewed action explicitly. Read
current host leases separately to inspect ownership, expiry and release state.
Lease lists support explicit next-page cursors. A failed reread clears the previous
current-read display; a saved acknowledgment remains historical. There is no
automatic acquisition, heartbeat, handoff or lease retry.


Lease command confirmations also retain the latest 256 terminal outcomes in private
app state, bound to the local store, project and actor. A later preview or endpoint
change does not turn an older completed handle into a new command. Retrying that
handle returns its recorded host, principal and time with `historical_only: true`
and `current_access_checked: false`; it never proves a current lease. A newer
prepared lease review remains separate. Known not-applied rejections have an exact
review-bound receipt so the dashboard can safely clear recovery and allow a fresh
explicit preview. An HTTP error by itself never clears the browser's saved lease
submission. Corrupt receipts fail closed, and evicted unknown handles never execute.
