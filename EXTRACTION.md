# Extraction provenance

Extracted on November 13, 2026 from the local SSG2 repository at commit
`43f3ad3fd0f5fca06a4c5e568a11d80598c8a1a3`.

Git fast-export/fast-import retained 14 commits affecting the selected 39
platform module, interface, test, fixture and design paths. Every selected HEAD
blob was compared with the source Git blob and matched exactly. Filtering changes
commit hashes while preserving the selected paths' relevant authorship/history.

The source checkout and its live database were left in place. Native binaries,
game assets, original application packages and SQLite/WAL state were not exported.
Migration of the existing research data is a separate backed-up acceptance step.

Standalone compatibility testing identified an additional runtime dependency,
`tools/task_graph.py`. It was copied byte-for-byte from the same source commit;
its source SHA-256 is `aeca4ba603a1ab66e291fdb8a5ce6b9ebc998a02def7ac83c7c133eb7ee27fcd`.
