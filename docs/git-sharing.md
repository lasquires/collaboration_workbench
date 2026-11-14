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
SQLite files, credentials or physical source roots. Safe task journals carry exact
text and source provenance. Other audit history uses stable event metadata and
payload digests. The serializer rejects host paths in portable record text;
unsafe task journals require the explicit disclosure review below. Use
project-relative references for new shared records.

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


## Removing relationships

Task-link removal participates in the same ancestry-aware merge as edits. A link
present in the reviewed common snapshot and absent from a later full snapshot can
be removed. A snapshot with no known common base cannot delete a local link just
by omitting it. Immutable research records and journal entries do not acquire
row-deletion semantics.

Removal versus a concurrent change to the same link requires an explicit choice.
The conflict preview labels that alternative `$presence` and includes the original
row, local row or absence, and remote row or absence. Choosing either side keeps
the alternatives in the immutable import review. Field choices remain pinned to
the reviewed remote tip and local service revision.

Git clients compare against their own last accepted snapshot, even if several
remote commits have arrived since then. The preview records that `merge_base_id`
separately from the incoming snapshot's original `ancestor_id`; the original bundle
hash and raw history remain unchanged. A first-time connection has no assumed
common base and requires explicit bootstrap review. Rewritten/deleted Git history
is rejected before applying newly fetched data.

The task's compatibility `dependencies` list mirrors the final reviewed dependency
graph. It is rebuilt from those links after applying choices, so choosing to keep
a dependency cannot leave its task fields saying it was removed. Original task
records and the incoming snapshots remain unchanged in history.

## Explicit review of journal sharing

Open **Sharing review** in the dashboard after choosing the project. Read the
original note on demand, inspect the safe public proposals, and choose a redacted
copy or omission notice for every affected note on the page. Nothing is selected
by default. Acknowledge durable publication, then save. Pages inspect up to twenty
journal entries; **Next journal page** continues without silent truncation.
Previously approved modes are shown as history and are not new default choices.

A stale save requires **Load fresh review · discard choices**. Reloading a dirty
review is explicitly labeled as discarding its selections. An uncertain save
locks those choices and provides **Retry exact same sharing request**. Its prepared
request IDs and modes are kept in this tab's browser session, without original
note text or credentials. Reloading the page restores the original project and
review; it does not automatically submit. Retry with the original authorized
access until the server confirms that request. Changing access cannot establish
whether an earlier uncertain request committed. Browser-session recovery is not
an export of local data or permission granted by an imported disclosure.

An authored note containing a local path pauses portable export. Read
`GET /v1/journal-sharing-review?project_id=...` to inspect a bounded page of safe
public proposals. The preview does not repeat matched private text. Each affected
entry offers **redacted** and/or **omitted** proposals; there is no default choice.
Redaction replaces each unsafe string value with `[LOCAL_PATH_REDACTED]` while
keeping safe sibling values. Omission publishes an explicit withheld-entry notice.
Neither edits the original note. Unsafe identity/attribution metadata and unsupported
non-journal fields keep export blocked.

Submit a normal scoped transaction containing:

```json
{
  "op": "review_journal_sharing",
  "review_digest": "the exact preview digest",
  "limit": 100,
  "cursor": null,
  "choices": {"original-event-id": "redacted"}
}
```

Use the preview's actual `limit`, `cursor` and event IDs, choose exactly one offered
mode for every entry on that page, and use the current transaction revision.
The caller needs current `contribute` and `review` capabilities. A changed source,
page or revision returns 409 and requires a fresh review. Exact request replay is
idempotent and still checks current authority. Read a fresh page after applying
choices; old page cursors expire. `current_choice` is present only when the local
receipt matches the current exact proposal.

A consent receipt stays on this installation. Peers receive immutable derived
`journal_disclosures` with original event/task/installation identity, attribution,
original source hash and a separate digest of the public projection. That source
hash refers to the withheld original; it does not authenticate absent original
bytes or confer forensic acceptance. Imported disclosures never create membership,
review rights or permission to share an unsafe original held by the receiver.
Task history/context labels derived entries and permits append-only corrections
that refer to the original event or disclosure ID. Local history keeps the raw note
and does not duplicate its public projection.

Publishing is durable: a later local choice cannot erase a disclosure already
shared through Git or a live host. Omission is a visible coverage gap, not a claim
of complete original history. Git/live status distinguishes local
`sharing-review-required` from `host-sharing-review-required`; the host owner must
review its own blocked notes. Generated roots, raw blobs and credentials remain
excluded. Upgrade collaborators to schema16 before exchanging this new table;
older snapshots without it remain readable without rewriting their hashes.
