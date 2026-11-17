# Resuming an expired worker claim

Repeating a legacy claim label preserves ownership and reservations but does not
renew its lease. The worker helper previously returned an expired brief after a
successful-looking `claim`, leaving later handoff operations unable to proceed.

The helper now reads the resulting lease. If it is the same worker's expired
claim, it sends an explicit, revision-guarded `heartbeat_claim` and reads back
the renewed state. It raises if the result remains expired. A different worker's
ownership is still protected; expiry never authorizes claim theft. HTTP/revision
failures are surfaced for reconciliation rather than silently retried.

`python -m unittest tests.test_worker_claim_renewal tests.test_worker_relay tests.test_worker_source tests.test_scoped_receipt_history`
passed4 checks through disposable HTTP fixtures. The new test fails on the old
helper, renews the same claimant without sleeping, preserves reservations, and
checks another label receives409. It alters lease time only in its temporary
fixture database. No production data or schema migration is involved.

This fixes a separate helper contract, not SQLite writer contention. It does not
add an automatic background heartbeat; long-running workers must explicitly
refresh their claim at task checkpoints.
