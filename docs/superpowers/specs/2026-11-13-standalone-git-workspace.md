# Standalone research workspace with Git connections

## Delivery boundary

The user clarified on November 13 that this is a separate application which
interfaces with Git and updates with Git. SSG2 is its first connected project,
not the application's source repository, brand, installation directory or data
directory. This extends the existing collaboration design and preserves its
evidence, review, conflict, history and recovery requirements.

The current Python service and browser interfaces are reusable implementation
material. Finishing their features alone does not satisfy this delivery boundary.

## Approaches considered

1. **Independent local application with connected repositories (recommended).**
   Reuse the local service and browser UI, package them independently, and connect
   arbitrary working directories. Local state remains outside the repositories;
   reviewed portable research records travel through Git. This supports multiple
   projects without requiring a hosted service or putting application code into
   each project.
2. **Install the dashboard into every repository.** Easier initial packaging, but
   couples its upgrades to each project's source tree and repeats the current
   embedding problem. It does not meet the clarified delivery boundary.
3. **Hosted application synchronized with Git hosting providers.** Useful later
   for remote teams, but adds hosting, authentication and provider dependencies
   to the currently approved local workflow. It is not required for this delivery.

## Recommended arrangement

- Give the application an independent source repository and launcher. Preserve
  attribution and the relevant SSG2 development history during extraction.
- Keep the browser UI and loopback service for the first release. "Separate app"
  means independently runnable and reusable; a native desktop wrapper can be
  added without changing the storage or Git connection contracts.
- Store connections, the local SQLite database, byte cache and runtime logs in
  an application data directory. Repository paths are local connection settings,
  never portable authority imported from someone else's clone.
- Connect a project by selecting its Git working directory. Its portable project
  UUID identifies the shared research workspace across clones; its local path
  does not. An existing workspace must retain its original UUID during migration.
- Ship project-neutral UI, help and APIs. SSG2-specific native evidence and
  restoration instructions remain project content.

## What updating with Git means

There are two independent flows. The user confirmed on November 13 that both
must be supported: code/assets refresh and shared tasks/research synchronization.

### Source updates

Detect changes to the connected working directory's HEAD, branch, index and
working-tree state on a bounded refresh interval and on explicit refresh. A
commit, pull, checkout or local edit refreshes repository status. Relevant source
changes enqueue indexing into immutable, hash-pinned versions; they do not rewrite
previous observations or promote findings to verified. A branch switch leaves
old evidence available with its original commit/build references.

The app does not need to perform Git network operations to notice changes made
by another Git client. Any app-initiated commit, pull or push must be a separate
explicit user action with visible repository and affected files.

### Shared research updates

An opt-in metadata directory in each connected repository holds portable project
identity and deterministic text records. The app saves reviewable exports there;
Git transports those files through ordinary commits and pulls. SQLite, local
leases, private connection paths, capabilities and delivery queues are excluded.

On repository changes, validate incoming files and compare them with the last
accepted snapshot and local state. Show a change summary. Compatible changes may
be applied through the service's transaction boundary according to the user's
sync preference; conflicting fields always require explicit choices. Keep losing
values and provenance in history. Never merge SQLite files or silently replace
local records with a pulled snapshot.

Large source bytes should be recovered from their pinned repository revision or
an explicitly configured artifact location rather than repeatedly embedded into
every Git metadata update. Missing bytes remain visibly unavailable; a content
hash alone does not certify native authenticity.

## Migration and acceptance

### Project-agent onboarding

The user confirmed that project agents may set up their project's integration
from platform-provided instructions. Ship a reusable onboarding guide, an
idempotent setup command and a diagnostic command. Setup creates only the
project's portable connection manifest and a small agent-instruction reference;
application source, runtime dependencies and local databases stay outside the
project. Show the exact project-file diff before applying changes to existing
instructions. Preserve unrelated AGENTS.md content.

The guide must explain how to connect the local clone, reuse an existing portable
project UUID, start or find the local service, use the scoped CLI, refresh source
indexes, export shared research and reconcile incoming Git updates. Diagnostic
output must identify the project/repository/branch, service and protocol versions,
sync state, missing source bytes and actionable setup errors. No project agent
should need to understand application internals, edit SQLite directly or guess
installation-specific absolute paths.

Demonstrate onboarding using an otherwise unconfigured second repository and
verify it using the published guide and commands. SSG2's integration instructions
then become the first real consumer of the same contract.

Create a consistent backup before migration. Import the existing workspace into
the application's data store without changing its project/task UUIDs, aliases,
source hashes, observations, decisions or history. Preserve the original store
and source files for recovery. Keep platform development in its own project so
SSG2 restoration workers see game work by default.

Acceptance requires running the application from outside SSG2, connecting SSG2
and a separate temporary repository, detecting source changes and branch
switches, exchanging research through two clones, handling concurrent edits
without loss, restarting and restoring a backup, and proving that the SSG2
restoration sources and native evidence were not modified by extraction.

No readiness claim is valid until the existing feature gates and these separate
application/Git integration gates pass.
