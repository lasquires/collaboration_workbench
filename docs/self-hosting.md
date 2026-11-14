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

## Current boundaries

Live HTTP authorization and TLS are implemented and tested. Local source
tracking and portable Git merge are separate operations. Automatic switching
between a live host and an offline client, reconnecting queued edits, and the
complete shared-sync UI still have their own acceptance checks; enabling the
listener does not establish those checks have passed.
