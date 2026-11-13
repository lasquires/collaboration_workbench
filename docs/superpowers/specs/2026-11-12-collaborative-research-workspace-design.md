# Collaborative research workspace

Design proposal, November 12, 2026. The user resumed work on this collaboration environment. This document specifies the intended extension; it does not claim implementation. It supersedes the earlier task-only roadmap design for future product scope while preserving its implemented migration and transaction work.

## Purpose and user requirements

Give humans and agents a shared research environment: browse the project, find useful work, investigate it, publish discoveries, and benefit when another investigation supplies a missing piece. Puzzilla is the interaction reference for exploring branches and research opportunities; the underlying project database plays the role of the shared research repository.

The user requires nested tasks, disconnected discoveries, directory-specific context, shared instructions, variables and lookout requests, help boards, discussion, document usefulness feedback, and deterministic notifications when newly indexed information may help. Humans and agents must see the same records. Markdown may hold instructions, explanations and diaries; current workflow state and structured findings belong in the database. No model needs to continuously watch for matches.

## Architecture decision

Extend the existing Python API and SQLite store with normalized relational tables. This is the recommended design choice, not a claim that the user selected a database product. Relations are stored as typed edges; the interface renders selected neighborhoods as a graph. A separate graph database adds deployment and migration work without being necessary for scoped traversals and indexed equality matching. Markdown/JSON as the write authority would require rebuilding transaction, identity and concurrency behavior already present in SQLite.

Start with one project service and one authoritative database. Put a project ID on every project-owned record from the start. A later centrally hosted service may use PostgreSQL behind the same API. Do not open several writable SQLite copies on different computers or synchronize them with Git. Cross-project knowledge requires explicit sharing grants and provenance; it is not enabled implicitly by identical identifiers.

## Existing foundation and gaps

The repository has a transactional task store, lossless legacy import, append-only events, revision checks, request IDs, a graph/dashboard and an HTTP CLI. Task coverage is partial. Variable occurrences and wishes still live in a separate JSON registry. The current graph is a prototype; a fit-to-bounds correction has fixture coverage but its final browser acceptance remains unfinished. Existing actor labels are not authenticated identities. These are migration inputs, not evidence that this design is already delivered.

## Records and identity

Use immutable opaque IDs internally, with existing task/VO/VW IDs retained as unique project aliases. Every editable record has a revision, author attribution, creation time, last-change time and archive state. Timestamps for ingestion, source creation and actual discovery are separate; unknown historical dates stay null.

| Record | Required meaning and relationships |
|---|---|
| Project | Goal, acceptance criteria, entry documents, coverage declaration and membership |
| Area | Directory or subsystem, parent area, path bindings, shared guidance and maintainers; an area may span several directories |
| Task | Purpose, scope, acceptance checks, state, owner/claim, reserved paths, primary parent, area and evidence links |
| Source artifact/version | Logical artifact identity plus immutable bytes hash, repository/revision or binary build identity, availability and locator |
| Index run | Input artifact versions, extractor/version/configuration, coverage boundaries, output manifest, errors and publication state |
| Occurrence | A symbol/key/field/method occurrence, its namespace and scope, exact source locator, index run and raw extracted value |
| Finding/revision | A specific claim with evidence, scope, epistemic classification, uncertainty and links to occurrences; alternatives may coexist |
| Question | Missing information, expected answer/evidence, structured match criteria, owning area and state |
| Task blocker | Links a task to a question, prerequisite or review gate; several tasks may share one question |
| Hint | A potential question–occurrence/finding connection, matched versions, rule version and explanation |
| Subscription/inbox item | Actor follows a task/area/question/variable; delivery state is separate from the shared hint's review state |
| Document/version | Instructions, reference or diary content, immutable content hash and linked projects/areas/tasks |
| Discussion/proposal | Comments on a record or document version, reply links, suggested changes and review decisions |
| Feedback | Helpful/unhelpful vote with reason and document version; one mutable vote per actor/version |
| Event/outbox | Append-only change record plus durable work to dispatch, match or notify after commit |

Task containment is a forest: one primary parent, unlimited finite depth, no cycles. Dependency edges are a separate acyclic graph. Related, evidence and candidate-match edges may be cyclic. Cross-area relevance uses links rather than duplicated tasks. Renames preserve IDs and aliases; merging duplicates requires an explicit reviewed redirect with history. A raw variable name is never a global identity: `strength` in two classes or binary builds remains two occurrences unless evidence establishes a connection.

Occurrence identity is unique on project, immutable artifact version, identifier namespace, canonical scope and canonical locator. Locators include a member/element discriminator where several observations share an address or span. Index-run membership is a separate relation, so reindexing the same source does not mint duplicate identities. Corrected extracted values create occurrence revisions with extractor provenance; a changed source artifact creates new occurrences linked by an explicit successor relation. Conflicting extractor observations are retained, not overwritten. Subscribe to occurrence IDs, questions or explicit scoped selectors; there is no global variable entity inferred from name equality. Approved alias edges can relate occurrences without merging their identities.

Sources support individual claims and relationship decisions. Evidence links name the claim, immutable source version, locator, observation method and limitations. Unsourced research questions are valid and visibly labeled; unsupported assertions cannot acquire verified status from votes or task completion.

## Collaborative structure edits

Extractor observations have their own immutable IDs under each occurrence. Publication order never selects an authoritative value. Conflicting observations coexist, each pinned to its run and evidence; a correction or preferred interpretation requires an explicit reconciliation decision linking the observations it supersedes. Match candidates pin observation IDs and evidence revisions, and unresolved disagreement is included in the hint. Publishing another run alone cannot invalidate previously accepted evidence or mark existing hints stale.

Every project contributor may propose and perform authorized add, detach, archive, move and merge operations on subtasks. Require an explanation and source references where the edit asserts a factual relationship; structural housekeeping may cite the duplicate task records themselves. Preserve actor, before/after records and rationale. There is no destructive deletion of referenced history.

Detach removes containment and leaves an island. Archive preserves the task and incoming references; active claims must be released or explicitly handed off first. Moving requires old/new parent revisions and cycle checks. Completed ancestors must be reopened before changing required scope. These operations remain distinct from the map's navigation-only Focus here action.

Merge uses a preview and explicit field-resolution choices. Select a survivor, retain both histories, union source links, deduplicate relationships, and review children and dependencies for cycles or newly self-referencing edges. Do not silently discard conflicting acceptance, ownership, findings or validation. Active claims require an explicit handoff; conflicting reservations block the merge. Old IDs redirect to the survivor. Record a reversible merge event: reversal restores the pre-merge records/edges if no intervening conflicting edits exist, otherwise require a compensating-edit preview. Do not automatically merge hints or change their review decisions; preserve original targets and expose redirects.

## Task lifecycle and collaboration

Preserve ready, active, blocked, review and done states; display blocked as Stalled with its specific reason. Archiving is separate from status. Claims record actor, lease expiry and heartbeat; expiry produces an abandoned-claim review signal, not automatic reassignment while edits may still exist. Enforce overlapping path reservations atomically. Each agent session belongs to a stable principal so repeatedly spawning agents does not create unlimited voting identities.

Any authorized contributor may create subtasks under their current task, including a proposed investigation. Creating a child does not automatically claim its files. Creation beneath a completed parent requires reopening the parent in the same transaction or creating a related follow-up outside its completed scope. Parent completion requires its own evidence gate; child counts are an informational progress measure only. Show completed/total required children and unknown decomposition, not a fabricated percentage of total effort.

Workers obtain a scoped context package, claim eligible work, journal meaningful steps, publish findings and questions, attach validation, then release or hand off. Two unsuccessful attempts at the same evidence boundary trigger the existing stronger-model routing policy; count attempts against that boundary, not every command failure. A new hint makes a stalled task worth reviewing; resumption, reassignment and completion remain explicit decisions.

Store `required_for_parent` on containment, `decomposition_complete` on the task, and structured attempts with boundary ID, outcome, resolution state and evidence. The shared progress projection reports direct required/optional child counts by workflow state, distinct descendant tasks with unresolved failed attempts, live/expired claims, and decomposition coverage. A completed node is green only when its own gate passes. Otherwise partial required-child completion gives yellow; a live active claim adds the green ring independently. An unresolved failed attempt marks its own node red and increments ancestor badges once per affected descendant task. Review and blocked badges remain visible. Expired claims lose the activity ring and gain a handoff-needed marker. UI clients consume this projection rather than independently inventing rollups.

## Deterministic indexing and matching

1. An indexer creates a run pinned to source versions and extractor configuration. Upload in bounded idempotent batches to staging; incomplete runs are not searchable as complete indexes.
2. Validate the manifest, locators, schema, scope and declared coverage. Publish the run atomically, retaining rejected records and errors for inspection. A completed partial index must explicitly identify the ranges/classes/files it did not cover.
3. Publication writes durable outbox events in the same transaction. A bounded matcher consumes events with persisted cursors and retries.
4. Match both directions: new observations against open questions, and new/edited questions against existing observations. Editing a rule triggers a versioned replay job.
5. Upsert a hint using `(project_id, question_id, criteria_revision, candidate_type, candidate_id, evidence_revision, rule_id, rule_version)`. Record full presentation revisions separately. Group related candidates from the same run into a digest instead of notifying per symbol.
6. Deliver inbox items to subscribed actors and affected task owners. Repeated processing does not duplicate hints or deliveries. Check permissions before matching and before delivery.

Initial rules use exact identifiers plus explicit scope constraints: symbol/class, plist key path, artifact/build hash, native mapping ID, type/offset pair or an approved alias relation. Preserve case and punctuation unless the identifier namespace defines normalization. Broad names alone may produce a low-specificity candidate only when the requester opts in. Index relevant fields so matching narrows candidates instead of comparing every question with every finding.

Each hint stores rule ID/version, compared fields, scope constraints, candidate/source versions and a human-readable reason. No opaque confidence percentage. Semantic search may later offer a separately labeled suggestion channel; it is not required for the first release and cannot silently create accepted aliases or meanings.

Hint states: new, investigating, useful, dismissed, stale. Question states: open, investigating, resolved, withdrawn. Resolving a question requires an answer record and evidence; marking one hint useful alone does not resolve it. Dismissal records a reason and applies to that candidate/question revision; materially changed evidence can create a new hint. Retracted or superseded evidence marks dependent hints stale and creates a review notice for accepted conclusions. It never erases the prior decision.

Increment criteria revisions only for structured matching-field changes and evidence revisions only for changes to source support or claim content; editorial text changes use presentation revisions. Replays retain prior decisions for the same semantic match fingerprint. A rule upgrade with unchanged matched fields/support carries dismissal forward; a changed fingerprint creates a linked successor hint explaining the difference. Deduplicate delivery by recipient, semantic hint identity and meaningful state transition. Store the matched values and evidence hashes so this decision is reproducible. New evidence never silently overwrites a dismissal; both the prior decision and the successor remain inspectable.

Durability semantics are at-least-once delivery with idempotent effects, not a promise of exactly-once execution. Crash tests must cover commit-before-dispatch, dispatch-before-ack and partial batch ingestion. Matcher failure leaves findings published and shows processing lag; it does not lose them or block ordinary task reads.

## Human interface

The main discovery dashboard is a Puzzilla-inspired branching map, with a project/area navigator and detail panel. List and board views are secondary alternatives. Preserve selected node, expanded branches, filters, pan and zoom when updates arrive.

Hovering or keyboard-focusing a node opens a compact preview: title, purpose, exact completed/total required-child counts, active and stalled counts, latest relevant hint, and current owner. Clicking pins details. On touch, tapping opens the same preview; a second explicit action opens detail. Preview controls must remain reachable without holding the pointer precisely on a small node.

Each node has an explicit Focus here action: make that task the displayed root and reveal its descendants to the chosen depth. This changes navigation only, never the task's real parent or dependencies. Keep a breadcrumb and Back control to return to the prior focus and viewport. A separate authorized reparent operation changes actual structure and must not be confused with navigation.

Use the user's requested visual language: solid green for completed and accepted work; a green outer activity ring for actively claimed work; yellow for partial required-child completion; red on the specific failed subtask. An ancestor with a failed descendant gets a small failure-count badge rather than turning the entire branch red. A red failure marker describes the latest unresolved attempt, not permanent task failure. Stalled work gets a pause/blocked badge with its reason; unstarted work is neutral; review has a distinct outlined marker. A partially complete parent can have a yellow fill and green activity ring simultaneously. Ring animation indicates activity only, never estimated percentage; provide a static equivalent for reduced-motion preferences. Shape, icon and accessible text accompany every color.

Progress previews show exact counts and flag optional children and incomplete decomposition. A leaf has its own acceptance state rather than a manufactured child percentage. Unknown, failed-attempt, active-claim and completion state are distinct data dimensions; derive their visual indicators consistently for both the map and API. Users can expand/collapse branches, change depth and filter for help requests or new hints without losing their navigation context.

The Puzzilla-inspired map starts at a selected goal/task, exposes a chosen depth, supports collapse/recenter and shows unknown branches. Containment is visible by default; users reveal dependencies, questions or shared variables as overlays. Disconnected findings have a visible Unplaced collection. Color plus shape/text distinguishes workflow states; badges show new hints, help requests and evidence concerns. Dense graphs load neighborhoods, not the whole database.

Selecting a task exposes Overview, Subtasks, Knowledge, Questions, Discussion and History. Overview answers what to do, what is known, what blocks it, what to read, and how to validate. History contains journals and raw change events without overwhelming the default view. A help board groups requests by area, evidence needed and requested capability. An inbox explains each hint and supports inspect, dismiss with reason, or attach as relevant evidence.

Document views show scope, version, usefulness feedback, discussions and proposed edits. Votes rank usefulness, never forensic truth. Feedback remains version-specific; show sample size and rationale. Comments anchor to a document version and optional heading/text range; changed anchors become outdated rather than silently moving. Accepting an edit creates a new document version and preserves the discussion. Markdown diaries may remain files; register immutable versions and links in the database. Do not create independently editable file and database copies of the same document body.

## API contract

All views use the same versioned API. List endpoints support cursor pagination, filters and explicit coverage metadata. Mutations carry actor identity, expected record revision and idempotency key; multi-record operations commit atomically. Initially retain the existing global revision for compatibility, then introduce per-record revisions with explicit aggregate checks to avoid unrelated edits conflicting. Never silently reinterpret existing clients' revision semantics.

Proposed resources:

- `/v1/projects`, `/areas`, `/tasks`, `/tasks/{id}/children`, `/tasks/{id}/context`
- `/v1/tasks/{id}/claims`, `/blockers`, `/validation`, `/events`
- `/v1/artifacts`, `/index-runs`, `/index-runs/{id}/batches`, `/index-runs/{id}/publish`
- `/v1/occurrences`, `/findings`, `/questions`, `/hints`, `/subscriptions`, `/inbox`
- `/v1/documents`, `/documents/{id}/versions`, `/discussions`, `/proposals`, `/feedback`
- `/v1/transactions` for atomic composed changes; `/v1/events?after=CURSOR` for resumable updates

Task context responses include direct and inherited instructions with provenance, acceptance, dependencies, blockers, relevant findings, unreviewed hints, reservations and recent handoff/validation. State the snapshot revisions, coverage and omitted/paginated sections. Area inheritance never promotes a child-specific inference into project-wide guidance. Agents fetch linked detail on demand; context packages are bounded, deterministic projections, not generated summaries that become another authority.

Start UI updates with cursor polling; add server-sent events if useful. Clients reconnect from cursors and reconcile drafts explicitly on conflicts. Search uses database full-text indexes for prose and typed fields for precise lookups. A query returning no hits reports index coverage; absence from an incomplete index is not absence from the original game.

## Permissions and boundaries

Local trusted mode can support the current single-user workflow, but labels must be visibly distinguished from authenticated identities. Before multi-computer deployment, require authentication, project membership, actor-scoped permissions, same-origin/CSRF protection, rate/batch limits and audit attribution. Indexers may publish observations; they cannot mark their own interpretations as independently verified by changing a flag. Accepted claims require the configured evidence/review gate. All contributors may create scoped subtasks and discussions; accepting project guidance or cross-project sharing is a separate capability.

Documents, extracted strings and comments are untrusted content, not tool instructions. Do not auto-execute linked commands. Resolve artifact paths through registered repository roots, reject traversal, and retain build/source hashes. Cross-project hints must not leak even the existence of private records. Shared databases belong behind a service; database files are not a distributed collaboration protocol.

## Migration and delivery sequence

1. Finish and checkpoint the current task-store cutover, including the unfinished graph browser check and legacy task coverage audit. Keep its exact archive, IDs and event history.
2. Add project/area identities, artifact versions and index-run tables. Import the existing variable occurrences/wishes losslessly, preserving VO/VW aliases, unknown dates, uncertainty and source evidence. Use dry-run reports and exact field comparison; switch authority once, never dual-write JSON and SQL.
3. Deliver one complete loop: publish a scoped occurrence, match an existing question, show an explainable hint in the task inbox, record its review and preserve it after restart. Include reverse matching for newly created questions.
4. Add the expandable task workspace, area/context browsing, help board, scoped subscriptions and document discussion/feedback.
5. Add authenticated remote service deployment only after local behavior and restore procedures pass. Migrate to PostgreSQL only when deployment/concurrency needs justify it; preserve API identities and events.

These are delivery dependencies, not live task statuses. Implementation tasks belong in the database after this design is accepted.

## Acceptance scenarios

- A new agent lists eligible work and retrieves sufficient scoped instructions/acceptance without scanning the project diary.
- A contributor creates three nested tasks and an island; another client sees them, their relationships and claims from the same records.
- A published index entry matches a preexisting lookout request; creating the request after indexing yields the same candidate and explanation.
- Identical variable names in unrelated scopes do not become one variable or an accepted cross-scope hint.
- Retrying a batch or crashing/restarting the matcher produces no duplicate observations, hints or notifications.
- Two distinct questions/candidates with revision 1 cannot collide. Reindexing identical sources preserves occurrence IDs; changed source versions remain distinct.
- Editorial edits and identical rule replay preserve dismissals; changed match criteria or evidence produce an explained successor without duplicate delivery.
- Add/detach/archive/move/merge preserve sources and history, reject cycles/claim conflicts, and support reversal or an explicit compensating-edit review.
- Map progress counts, activity rings and failure badges agree with the API projection across nested optional children, expired claims and incomplete decomposition.
- Retracting evidence alerts tasks that relied on it without automatically rewriting their conclusions.
- Two simultaneous claims or conflicting path reservations yield one owner; stale edits retain drafts and require reconciliation.
- A useful hint does not complete a task, and completing all children does not bypass the parent's acceptance gate.
- Document edits preserve old comments/votes on their actual version; usefulness votes cannot change evidence classification.
- Graph, list, CLI and context endpoint agree on IDs, statuses and revisions; large datasets are paginated and neighborhoods bounded.
- Restoring a backup recovers tasks, findings, hint decisions, outbox cursors and source-version references consistently.
- Project boundaries prevent search, matching and notification leakage in remote/shared mode.

## Git collaboration and research presentation clarification

The user explicitly requests local Git support now; connecting a remote such as
GitHub can follow later. Preserve immutable repository/commit/path/blob references
for source claims, with optional line ranges. Clearly distinguish current dirty
working files from committed evidence. Reading repository status and resolving
sources must not stage, checkout, commit, fetch or push implicitly.

Cross-computer exchange must use a versioned, deterministic text bundle, not Git
merging live SQLite files. Export stable record identities, source references and
history with a shared ancestor identifier. Import previews additions, identical
records, concurrent edits and missing referenced sources. Apply through the same
revision-checked service transaction as ordinary edits. Preserve both sides of
conflicts until a contributor explicitly reconciles them; repeated imports must
be idempotent. A bundle cannot transfer an active local worker claim as a live
lease on another computer. Git transports the bundle; it does not provide live
presence or instantaneous synchronization. Demonstrate the exchange using two
temporary clones and conflicting edits before claiming multi-computer support.

The interface must communicate what contributors know, what they are testing,
and what remains unresolved. These need not be three literal tabs: choose the
clearest integrated task detail layout. Contributions have a stable contributor
identity and visible username, evidence and discussion. Verification attaches
to a bounded claim or behavior and must coexist with unresolved sibling claims.

## Decisions still open for implementation planning

## Source criticism and evidence-based reconstruction

Methodological references: the [TEI critical apparatus guidelines](https://www.tei-c.org/release/doc/tei-p5-doc/en/html/TC.html)
model distinct witnesses, variant readings and responsibility; the
[Genealogical Proof Standard](https://www.bcgcertification.org/ethics-standards)
emphasizes source citation, evidence analysis, resolving conflicts and reasoned
conclusions. The following are project design adaptations, not a claim of formal
compliance with either discipline.

- **Compare accounts:** show observations of one scoped behavior side by side,
  each attached to its exact source build and extraction/run. Preserve a selected
  interpretation alongside competing interpretations and why it was selected.
  A different APK version is a different witness, not automatically an error.
- **Trace derivation:** record `derived_from` links for extracted files, reports
  and claims. Several reports copied from one run are not independent supporting
  observations. Display their common origin rather than counting votes as proof.
- **Explain conclusions:** a verified claim needs its scope, supporting and
  contradicting evidence, experiment limitations and an explicit rationale.
  Distinguish a transcription correction from a changed interpretation. A later
  source does not erase the earlier reading or silently supersede a conclusion.
- **Record meaningful absence:** an unsuccessful search records artifact/build,
  query or method, searched range, coverage and outcome. Distinguish 'not found
  in this search' from 'does not exist'; incomplete indexing cannot prove absence.
- **Resolve identity cautiously:** matching names suggest candidates, not merges.
  Show build, namespace, class and location when comparing variables/functions.
  Cross-scope identity decisions require cited rationale and remain reversible.

Use plain interface labels such as Compare evidence, Source history, Why we
think this, and Still unexplained. Reuse observations, evidence links, decisions
and research events rather than creating a parallel scholarly database. These
are acceptance details for provenance and research UI slices: fixture cases must
show conflicting builds, common-source derivatives, a scoped negative search,
and a conclusion whose unresolved limitation remains visible.

Exact visual library, numerical batch/page limits and lease duration should be chosen through measured fixtures, not invented here. The initial runtime scope is one local project, with Git exchange between checkouts included. Hosted live collaboration and shared cross-project knowledge are separate deployment gates. Full semantic matching, autonomous agent spawning, automatic source execution and automatic acceptance of findings are outside the initial design.
