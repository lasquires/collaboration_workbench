# Sparse publication notification paging

Live SSG2 processing exposed 21 pending publication jobs, including jobs with
173 attempts and a full-membership cursor beyond 181,300. Whole-run Git indexing
was repeatedly spending the bounded matching budget on identifiers that could
not satisfy any group in the current question.

Publication paging now fetches only observations whose identifiers occur in the
question criteria. The existing matcher still checks namespace, owner, build,
value and all other scope constraints. No hint identity, evidence state,
question revision, replay rule or subscription is changed.

The durable cursor remains a question index times full run size plus a full
membership ordinal. A partially processed job from the earlier implementation
starts at that same ordinal; after processing a sparse page, its cursor advances
to the original rank of the last observation. Unrelated tails can be skipped.
Published run membership is immutable, so skipped identifiers cannot become
matches for that publication/question revision. Revised or newly created
questions retain their separate backfill events.

Validation must cover sparse publications, wrong-scope rejection, legacy cursor
restart, exact hint deduplication and the existing matcher suite before live
deployment. Backend checks do not establish UI delivery or original-game parity.

Validation checkpoint:16 existing matcher tests,8 ingestion tests,2 sparse publication/restart tests and1 query-reuse check pass. The sparse publication case contains82 observations, but only2 watched-identifier candidates consume its initial pair budget; wrong scope produces no hint. A legacy offset resumes with3 remaining pairs,3 new hints and no pending publication job. The query-reuse fixture was aligned with the already-committed EXISTS/count query and its imported-TestCase duplication/cleanup were corrected. Live deployment must be verified separately.
