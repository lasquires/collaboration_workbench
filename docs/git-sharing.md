# Project-owned Git sharing

Connect the project repository through Repositories first. Setup pins the project
UUID and its credential-free remote identity in `.research-workspace/project.json`.
The app never falls back to the Collaboration Workbench repository's remote.

Choose **Enable Git sharing** on the connection card to select portable tasks and
research for that project's data branch. This is separate from automatic local
source tracking. The default ref is `refs/heads/workbench-data`; ordinary source
branches and staged changes are untouched.

Edits persist locally immediately. Automatic jobs wait for a 30-second quiet
period and use a five-minute minimum network interval. Periodic checks also bring
in incoming changes. **Sync now** queues an explicit job and bypasses the wait.
An unchanged portable snapshot creates no commit or push. Queued requests and
accepted bases survive restart.

## Joining and resolving changes

An existing remote with an unknown common ancestor needs explicit review. Choose
**Review incoming Git changes**, inspect local and shared values, and select a
side for every conflicting field. Compatible additions and edits combine.
**Apply reviewed changes** checks the exact remote tip and local revision again;
if either changed, discard that preview and review again. It queues a normal
ancestry-preserving synchronization. No force push replaces another person's work.

Two independent app installations have distinct local actor IDs even when they
connect the same project UUID. Labels and GitHub names are attribution; they do
not authenticate a person or prove independent forensic review. Imported accepted
work returns to review when its support or scope changes. Live leases and local
capabilities are never imported through Git.

## Source metadata and local bytes

Portable version 2 carries source identities and hashes, not raw artifact bytes,
SQLite files, credentials or physical source roots. History uses stable event
metadata and payload digests; full original history remains in the originating
app and explicit local exchange bundles. The serializer rejects host paths in
portable record text instead of silently rewriting evidence. Use project-relative
references for shared records.

An imported source root is unbound on the receiving machine. The owner can bind
it using the scoped `bind_root` operation, then authenticate the local file with
`register_artifact_version` and its expected hash. Matching bytes hydrate the
existing immutable source identity. `local_bytes_available` reports actual local
availability; an imported historical availability label does not prove that this
machine has the source. Binding and hydration do not create another Git update.

## Agent API

All routes select `project_id` and `repository_id`. Shared hosts require an owner
credential for Git jobs because they use host Git credentials and filesystem state.

| Route | Request | Result |
|---|---|---|
| `GET /v1/git-sync-status` | query identifiers | Local enabled/state/result/error |
| `POST /v1/git-sync-configure` | identifiers, `enabled`, `export_selected` | Durable settings/status |
| `POST /v1/git-sync-now` | identifiers | Durable queued request |
| `POST /v1/git-sync-preview` | identifiers | Exact tip, bundle ID, local revision and alternatives |
| `POST /v1/git-sync-apply` | identifiers, `choices`, `remote_tip`, `bundle_id`, `expected_revision` | Guarded apply and queued sync |

Requests and responses are JSON. Choice keys are canonical JSON arrays of
`[table, row_key, field]`; values are `local` or `remote`. Never infer a winner
from timestamps. A remote binding change stops sharing until setup is reviewed.
Git-only sharing also serves as fallback for an unavailable configured live host.
See [live hosting](self-hosting.md) for backend configuration, conflict review,
and the remaining UI and shared-claim boundaries.

## Task journal history

Schema 9 adds immutable task journal records to portable exchange. Observation,
attempt, blocker, review and handoff entries carry the exact text and structured
evidence, boundary/outcome fields and correction references. Task history and
context include imported entries alongside local audit history without duplicates.
The original event JSON, its SHA-256 and installation identity remain available in
the exported record. Local events and their payloads are never rewritten.

Corrections append another entry referencing the original event ID. They do not
replace the original. An incompatible reuse of an immutable journal identity
requires a new identity; choosing the remote alternative cannot rewrite it.
Imported labels do not create memberships or capabilities. Claims remain local.

Upgrade all collaborators to the current app before exchanging the new schema.
The new app can read previous snapshots that omit the journal table; their hashes
and raw JSON remain unchanged. Historical journal entries can be recovered from
the installation that still holds their original local event log. A digest-only
snapshot cannot reconstruct text it never included.

Existing portable path checks also apply to journal text. If an entry includes a
machine-specific absolute path, automatic sharing pauses for explicit review;
it does not silently redact or alter the original note. Use repository-relative
evidence references in new shared notes.
