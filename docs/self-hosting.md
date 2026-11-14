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

Local-client claims remain **local-only**. Snapshot exchange does not create an
acknowledged host lease. Use the host directly for shared claim coordination until
lease relay is implemented. Portable event metadata preserves attribution and
payload digests. Schema 9 also shares immutable task-journal text, evidence, and
correction links through the same portable exchange.
Enabling a listener or completing synchronization never verifies research claims.
