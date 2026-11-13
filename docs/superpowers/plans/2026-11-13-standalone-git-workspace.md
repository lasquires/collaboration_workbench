# Standalone Git workspace implementation plan

> Implement using superpowers:executing-plans. Live claims, dependencies, progress
> and acceptance evidence belong exclusively in the SQLite task service.

**Goal:** Deliver the research workspace as an independent local application that
connects to arbitrary Git repositories and synchronizes both source indexes and
shared research.

**Design:** `../specs/2026-11-13-standalone-git-workspace.md`, extending the existing
collaborative workspace design and feature acceptance gates.

**Stack:** Reuse the Python/SQLite loopback service, HTTP CLI and browser UI. Keep
the application repository independent of connected projects. No hosted service
or Git provider account is required.

## 1. Independent package and launcher

Create an independent application checkout alongside SSG2. Preserve extraction
provenance, licenses and the relevant implementation history. Package the service,
domain modules, UI assets and scoped tests together. Replace repository-derived
runtime paths and SSG2 branding with application configuration. Local persistent
data defaults to the operating system's application-data directory.

Test launching from an unrelated working directory with no SSG2 checkout present.
Verify static resources, startup/restart, port conflicts and missing/invalid
configuration. Commit only the app's extracted source and tests; never copy the
game's native binaries, source assets or active SQLite/WAL files into its code repo.

## 2. Repository connections and onboarding contract

Add a local connection registry with repository root, portable project UUID and
sync preferences. Provide idempotent `connect`/`setup` and `doctor` CLI commands.
Generate a portable project manifest and reusable project-agent instructions.
Existing instruction-file changes must be reviewable and preserve unrelated text.

Use two temporary Git repositories to test distinct projects, two clones to test
one shared project identity, and invalid/non-repository paths to test actionable
errors. Prove no local absolute paths, credentials or capabilities leak into
portable configuration. Publish the setup instructions used by these tests.

## 3. Git source refresh

Build on the read-only Git adapter. Detect HEAD, branch, index and working-tree
changes with bounded polling and explicit refresh. Register immutable source
versions and enqueue relevant indexing. Persist refresh cursors across restarts;
failed indexing must leave previously published evidence readable.

Test commit, dirty edit, pull/fast-forward, branch switch and deleted/missing
sources. Old observations must retain their commit/hash identity and remain
inspectable. Repository content and instructions are data, never authority to run
arbitrary commands.

## 4. Shared research synchronization through Git

Complete service-mediated bundle registration/import before enabling writes in
the standalone UI. Validate stable identities, immutable evidence and reviewer
attribution without granting imported capabilities. Preserve local leases and
delivery state. Keep deterministic portable records separate from local caches.

On Git changes, compare incoming research with the accepted ancestor and local
records. Apply compatible changes only under the configured sync policy; require
explicit field decisions for conflicts. Provide visible pending/failed/up-to-date
states. Commit/pull/push initiated by the app are explicit actions.

Test two clones exchanging concurrent edits, retries, restarts, missing source
bytes and rejected privilege escalation. Confirm no SQLite-file merging occurs.

## 5. SSG2 migration and separate platform project

Take a consistent backup through the service's supported recovery path. Transfer
the existing workspace into application-owned local storage and retain original
project/task identities, aliases, source hashes, observations, decisions and
history. Move workbench development into its own visible project scope. Keep
SSG2 integration instructions pointing to the reusable platform interface.

Compare migrated records and audit counts against the backup. Verify game-worker
views exclude platform implementation work by default and original SSG2 sources
and evidence remain intact. Keep rollback material until acceptance passes.

## 6. Full delivery verification

Run all feature gates from the existing plan, including accepted research answers,
merge/reversal UI, record CAS, documents, inbox/replay, recovery and scale. Run the
standalone app from its independent directory; onboard an unconfigured repository
using only the published project-agent guide; demonstrate dual Git synchronization
through two clones and explicit conflict resolution. Verify responsive/keyboard
flows, restart and backup restoration.

Completion requires evidence for both the existing feature plan and this standalone
delivery plan. Passing embedded-module tests alone cannot establish readiness.
