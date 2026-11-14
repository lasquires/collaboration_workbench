# Authored research lifecycle implementation plan

> **For agentic workers:** Use superpowers:executing-plans for coordinator integration. A bounded read-only review follows each substantive deliverable.

**Goal:** Let people and agents publish project findings and evidence, then explicitly propose/review answers without losing prior interpretations or decisions.

**Architecture:** Immutable finding revisions retain separate semantic evidence and editorial presentation lineage. Exact source versions and locators support authored claims; classification is an author assertion and never automatic forensic acceptance. Evidence withdrawal has immutable events and project review notices. Durable publication outbox feeds the existing bounded matcher. Answer proposal, independent review and question resolution are a subsequent gate of the same lifecycle, not inferred from hint usefulness.

**Tech stack:** Python standard library, existing transaction/API/CAS layers and SQLite migrations; existing Git/live exchange.

**Spec:** docs/superpowers/specs/2026-11-12-collaborative-research-workspace-design.md.

## Constraints and review focus

- Preserve original finding/source versions, journal records and hint decisions. No direct source database writes.
- Scope every UUID/source/task/area/occurrence reference to its project. Authentication and transaction replay remain enforced by the owning store.
- Changes to claim, scope, classification or source support create a new semantic origin. Uncertainty text edits are editorial; they must not recreate hints or erase dismissals.
- Archived/superseded revisions and withdrawn support cannot become active candidates from a delayed outbox job or Git import.
- Published findings remain available when matcher processing lags or fails. No publication marks a question resolved.
- Portable rows contain provenance and authored content; host paths and ownership remain subject to existing sharing checks.

## Deliverable 1: finding authoring and evidence lifecycle

1. Write a failing public-store regression for create_finding, revise_finding, evidence reads and immutable prior revisions.
2. Add tools/workspace_findings.py for scoped operation validation, pinned source descriptors, immutable lineage/evidence-change/notice tables, bounded reads and public review labels. Upgrade schema11->12 with backup/fault rollback checks.
3. Integrate typed finding guards, durable semantic publication and current-origin matcher candidates. Preserve editorial dismissals and stale old semantic candidates atomically.
4. Add evidence withdrawal/supersession with explicit expected state, reason and optional replacement. Preserve source fields and history; never implicitly revive withdrawn links.
5. Integrate portable tables, lineage/history validation, and post-import publication/staleness so existing local questions see imported findings. Preserve incoming raw snapshots.
6. Verify scoped HTTP authorization, replay, cursors, raw-history migration, offline import, matcher/editorial behavior, full existing checks and independent review before publishing.

## Deliverable 2: evidence-gated answers and conclusions

Implement attributed answer proposals pinned to question criteria and immutable support; explicit independent review and selected answer resolution; retain contradictory decisions and require review when sources or question requirements change. Integrate Git/live sharing, review notices, scoped agent reads and human controls. This deliverable remains required even after finding authoring passes its own gate.

Task state, owners and evidence are recorded only in the source SQLite task workspace.

### Answer implementation contract

Answer proposals are immutable and pin the exact question requirements hash,
question revision, support link IDs and source digest. They may also attach direct
pinned source descriptors. New proposals express corrections or alternatives.
Review decisions are immutable and independent of the proposal author. A reviewer
may explicitly supersede only their own earlier decisions; conflicting current
review heads are disputed, never ordered by wall-clock time. An installation-local
receipt records that this service authenticated the review write. Receipts are not
portable, and imported review rows cannot grant local acceptance. Imported authored
resolution stays recorded, with a separate effective state showing review needs.

Resolving a question explicitly selects a fresh locally accepted answer. A mutable
current selection has immutable resolution events. Reopening or withdrawing never
erases prior proposals/reviews. Source withdrawal, a changed finding origin or
question requirements invalidates active selections atomically and emits a review
notice. Local capability revocation affects effective trust without rewriting
shared authored history. All new operation envelopes support scoped question CAS,
and reviewer credentials are checked both at HTTP and under the transaction lock.

New module: tools/workspace_answers.py. New checks: tests/test_workspace_answers.py
and authenticated shared HTTP cases. Schema12->13 adds answer contexts, reviews,
current selections, resolution events, and local-only review receipts. Portable
validators reject foreign support, forged context digests, invalid supersession
and dangling/contradictory selections; old unversioned answer rows remain explicitly
unreviewed instead of being silently promoted.
