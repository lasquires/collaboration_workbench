# Source Criticism Presentation Implementation Plan

> For agentic workers: use the existing subagent-driven development and verification workflow. Live ownership and acceptance belong only in the SQLite task workspace. This plan describes implementation contracts, not live task status.

Goal: complete the already accepted conflicting-witness, common-origin, scoped
negative-search and visible-limitations requirements without creating a parallel
research database. Keep the existing Evidence observation browser and two-record
comparison. Reuse finding revisions, pinned evidence, archive/restore and exchange
for derivation assertions. Schema and portable table sets remain unchanged.

## Boundaries and ownership

Coordinator integrates `workspace_findings.py`, `workspace_research_context.py`,
`roadmap_research.py`, `roadmap_server.py`, dashboard, research renderer, view/HTTP
fixtures, README and this plan. One worker owns only new
`tools/workspace_source_context.py` and `tests/test_workspace_source_context.py`.
The other owns only the research session and its Node test file. No overlapping
writes, index changes, branch changes or source authority migration.

## Typed derivation assertions

An existing finding may explicitly carry:

```json
{"artifact_version_id":"child-version","derivation":{"format":"workbench-derived-source/v1","parent_version_id":"parent-version","index_run_id":"optional-published-run"}}
```

Other scope fields and unknown derivation formats retain their existing meaning.
The exact marker opts into validation: both versions must belong to the project,
parent and child differ, and any cited run must be published and include the
parent version among its exact inputs. Source support must cite both versions
with reviewed locators, methods and limitations. Ordinary finding revisions and
source withdrawal retain original support and prior assertion versions.

The backend module exposes `is_derivation`, `validate_scope`, `validate_support`,
`validate_bundle` and `read`. Live creation/revision and portable import use the
same typed-reference constraints. Imported assertions never grant review rights,
prove provenance independently, or establish native behavior. Current, historical
and archived assertion versions remain browsable. No automatic identity merge,
preferred interpretation, confidence vote, or independence count is introduced.

## Exact source reads

`GET /v1/artifact-versions/{id}/context?project_id=...` returns an exact source
entity with pinned hash/build/availability and safe relative artifact metadata.
Its related descriptors select source derivations, observations and negative
searches by this exact artifact version. No local root binding is disclosed.

`source-derivations` requires `subject_id` and returns matching typed finding
revisions, including historical/archive state, finding identity and both exact
source-version tuples. Shared explicit run identities can be grouped within the
current page. A parent source view reveals its recorded derived children. Shared
binary hashes alone do not establish either dependence or independence.

Scoped observations expose only published runs. Scoped negative searches retain
query, method, searched range, coverage, outcome and attribution. All paged reads
use20 by default, maximum100, stable ordering and cursors bound to project,
resource, subject, limit and current store revision. Stale cursors require restart;
unknown parameters, foreign references and malformed identities fail explicitly.
Reads do not mutate workspace state or an authored draft base.

## Human workflow and exact recovery

The controller adds immutable `source` selection and related source reads. It also
supports the existing `record_negative_search` operation with a client-generated
stable ID, exact artifact version, query, method, searched range, explicit coverage
and outcome. Pending writes retain project/principal/source and exact serialized
body; reload never submits. A matching `negative_search` receipt is required.
Known stale rejection needs fresh source context and explicit reconciliation;
uncertain outcomes retain exact retry. No synthetic record revision is attached
to an immutable source version.

Source history is reachable from observations, findings, support and the pinned
version catalog. The derivation editor creates an ordinary finding assertion;
parent version and optional published origin run are selected explicitly. Source
methods, locators, limitations and assertion text are authored, never guessed.
Scoped search forms explain that an unsuccessful search is not proof of absence.
Comparison shows exact build/hash/location/value/provenance and warns when scopes
differ; no winner is chosen. Conclusion limitations remain visible beside claims.

## Test and integration sequence

1. Write failing helper/controller fixtures for exact source context, typed scope,
   support and import references, historical derivations, page identity and frozen
   negative-search requests. Implement only the owned files and run their suites.
2. Coordinator adds real HTTP rejection/scoping fixtures before registering routes
   and finding/import hooks. Verify ordinary unknown scopes and existing bundles
   remain compatible. Then integrate the renderer through existing guarded APIs.
3. Add DOM fixtures for source navigation, explicit parent/run selection, search
   coverage, common-origin labels, conflict comparison and draft preservation.
4. Use an authenticated browser fixture with two conflicting source builds, two
   reports citing one recorded run, a bounded negative search and a conclusion
   retaining unresolved limitations. Verify exact API identities and no automatic
   proof/identity inference, plus phone layout and literal text.
5. Run task-relevant Python and all Node suites, independent code review and the
   parent-specific acceptance gate before publication. Re-run a broad regression
   after backend integration. Keep source authority and project separation as
   separate backed-up gates.
