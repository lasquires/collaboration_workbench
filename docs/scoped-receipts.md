# Scoped transaction receipts avoid discarded full history

The v1 transaction path built a legacy `_snapshot(db)` after updating revision,
then discarded its tasks, links and complete event history when constructing
the scoped receipt. This work occurred while the SQLite write transaction was
open. The v1 response now reads the updated revision directly. Legacy callers
still receive the full snapshot; all history remains stored. Exact request
retries still return the original receipt.

`tests/test_scoped_receipt_history.py` fails on the old path and passes on the
new path. It checks that a scoped transaction and exact retry never call the
full snapshot builder, then verifies revision, receipt shape, stored task and
retained history. Existing store, transaction-outcome and HTTP API checks pass.
The first combined run passed25 checks and had5 Windows loopback-denial errors;
the socket-dependent outcome/API suites and new regression were rerun with
loopback access:19 checks passed. No failing application assertion was waived.

Commands:

```text
python -m unittest tests.test_scoped_receipt_history tests.test_roadmap_store tests.test_workspace_transaction_outcomes
python -m unittest tests.test_workspace_transaction_outcomes tests.test_roadmap_transactions_api tests.test_scoped_receipt_history
```

A disposable fixture with10001 events measured the discarded snapshot at
0.13145seconds and7328800 JSON bytes. The updated scoped journal transaction
took0.03490seconds and returned345 bytes. These are single local measurements,
not throughput guarantees or proof of the live503 cause. The synthetic history
was inserted only in a temporary fixture database, never production.

`docs/repeated-busy-diagnosis.md` records the separate5-second SQLite timeout
reproduction and competing writer boundaries. Reducing unnecessary lock-held
work does not prove all contention resolved. Deployment and pending worker
handoffs must be checked separately. No schema migration is required.
