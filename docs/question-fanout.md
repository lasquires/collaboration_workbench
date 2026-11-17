# Bounded notification pages for late questions

The live matcher held SQLite's writer lock in `_observation_at`, called by
`_process_event_page` for question events. Temporary same-service diagnostics
recorded repository-refresh and worker-heartbeat threads timing out at
`BEGIN IMMEDIATE` while that matcher thread ran its observation query. No SQL
text/task payloads were logged. The pre-deployment processing-status API showed
two pending question events at offsets15458/15387 and156/155 attempts, with no
failed or replay jobs.

The old small-watch shortcut queried matching observation identifiers but counted
all sourced findings, including unrelated identifiers. A large finding history
therefore disabled the shortcut. The fallback repeatedly ran correlated global
observation OFFSET queries while holding the page's write transaction.

Question pages now prefilter both published observations and eligible current
findings by exact identifier. They retain all existing scope/build/evidence
matching. Published observation membership is ranked once per query; candidate
positions preserve the old global order: observation IDs, then current eligible
finding revision IDs. Large sets resume that durable ordinal after restart.
Small sparse sets retain the previous restart-and-deduplicate recovery contract.
No authority data or schema migration is required.

`tests/test_question_sparse_matcher.py` checks unrelated histories, wrong-scope
rejection, mixed observation/finding hints, small legacy restart deduplication,
and a multi-page restart crossing from observations into findings. It forbids
the per-observation fallback in these cases. Existing matcher, ingestion,
publication paging and query tests remain required. Source eligibility, hint
identity, notification recipients and exact match rules are unchanged.

Validation command:

```text
python -m unittest tests.test_question_sparse_matcher tests.test_research_matcher tests.test_research_ingestion tests.test_publication_sparse_matcher tests.test_matcher_page_queries
```

The prior receipt-size reduction and lease-renewal fixes address separate issues.
The new paging change must be validated against live queue drainage and actual
worker writes before declaring the observed contention resolved.
