# Project-scoped task import storage implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline integration; bounded read-only review follows verification.

**Goal:** Connect independent projects with repeated task labels without changing UUIDs, public aliases, immutable source text, or another project.

**Architecture:** Keep global legacy SQLite keys private. An installation-local immutable mapping binds each imported task UUID to its stable exchange storage key. Exports use stable exchange keys; SQL writes translate them to collision-free local keys. Stored merge snapshots use exchange keys and are translated into local views for recovery, without rewriting their original bytes. Existing unbound tasks keep identity mappings and existing bundle formats/hashes remain valid.

**Tech stack:** Python standard library, SQLite, existing versioned exchange and transaction APIs.

**Spec:** docs/superpowers/specs/2026-11-12-collaborative-research-workspace-design.md; user requirements for multiple repositories and lossless Git/live synchronization.

## Constraints and review focus

- Task UUID/project identity and user aliases are authoritative; global SQLite keys are implementation details.
- Source journal hashes, original_json, accepted snapshots, raw events, and portable compensation audits remain byte-for-byte preserved.
- Local mappings never confer membership, import claims, or create content-only sync churn.
- Reject duplicate/foreign UUIDs and unstable exchange identities atomically.
- Repeated imports, later updates, link deletion, merge compensation, and a third peer must work after key remapping.

## Implementation

1. Add a real two-store failing regression: same aliases, different projects/UUIDs, parent/dependency links, full journal provenance; register the second project using the public API.
2. Add workspace_task_namespace.py: local mapping DDL, safe import allocation, row conversion, and snapshot conversion. Upgrade schema 10 to 11 with existing backup/rollback gates.
3. Translate exported tasks/links, validate by scoped UUID/canonical exchange ID, and translate selected import rows/deletions before SQL. Store original bundles unchanged.
4. Persist new merge snapshots in exchange namespace; translate views for strict reversal and compensation. Do not rewrite historical snapshots or audit payloads.
5. Verify repeat/third-peer exchanges, edits/deletions, recovery and migration; reject forged history or scope changes. Run focused and full suites, then bounded independent review and publish.

Live execution status and evidence belong only in the source SQLite task workspace.
