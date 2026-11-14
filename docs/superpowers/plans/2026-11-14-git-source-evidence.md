# Connected Git source evidence implementation plan

> **For agentic workers:** Use scoped subagent-driven development for the pure capture helper; coordinator integrates and verifies publication. Live task state is in the source SQLite workspace only.

**Goal:** Repository refresh pins actual committed code/assets and publishes bounded, explicitly scoped lexical source observations for tasks, findings and questions.

**Architecture:** Read immutable Git objects outside DB locks; verify original blob IDs and content SHA256, never substitute dirty worktree files. Coordinator publishes content-addressed project source versions and lexical observations through existing staging/manifest/outbox machinery, and preserves a portable immutable Git source manifest. Source and sharing states remain distinct. Same bytes at the same project source path reuse identity; local first-observed timestamps must not become semantic identity conflicts. No code execution or inferred native meaning.

**Tech stack:** Python standard library, bounded read-only Git subprocesses, existing SQLite/research/exchange APIs.

**Spec:** docs/superpowers/specs/2026-11-12-collaborative-research-workspace-design.md; user requires connected repositories and updates with Git, backend first.

## Fixed capture interface and ownership

Worker owns only tools/workspace_git_capture.py and tests/test_workspace_git_capture.py. Coordinator owns publication, schemas, watcher integration, exchange, docs and integration tests. Workers do not spawn workers or switch branches. No shared Git staging while ownership is active.

Function `capture_commit(root, commit, *, max_files=500, max_file_bytes=8*1024*1024, max_total_bytes=32*1024*1024, max_tokens=20000, max_seconds=25.0)` returns a dict with `format='workbench-git-capture/v1'`, pinned `commit`, `files` and `coverage`. Each file has `path`, `mode`, `blob`, `size`, `availability` (`available` or `excluded`), optional `reason`, `sha256` and `body` (private bytes only when available), and `tokens` list. Token fields: `identifier`, `byte_offset`, `byte_length`, `line`, `column` (one-based line/Unicode column; byte offsets authoritative). Include `text_state` such as utf8/binary/too_large. Files sorted by raw Git path ordering, tokens in source order. Non-UTF8 or NUL-bearing bodies remain pinned binaries without lexical interpretation. Preserve Unicode identifiers/case. Coverage includes booleans `complete`, `pinned_complete`, `lexical_complete`, explicit `excluded` entries/reasons, total path/byte/token counts. A literal comment/string token is lexical evidence only. No local absolute root in returned portable metadata.

Only full lowercase 40/64-hex commits accepted. Bound ls-tree output, all subprocesses and whole capture deadline. Regular 100644/100755 blobs only; do not follow symlinks/submodules. Verify Git blob object hash (SHA1 or SHA256 as appropriate) and content SHA256. Dirty files are ignored. Reject unknown commit/protocol corruption with GitCaptureError (may subclass watcher.GitWatchError). Resource exclusions are explicit partial coverage, not silent success. Existing watcher._run may be reused; any helper changes require coordinator agreement. Read-only commands must disable optional index writes, external filters/diff hooks; never run project code.

## Coordinator integration

1. Write a failing initial-refresh/commit-update regression that checks pinned bytes and indexed source-token identifiers, old-version retention and no-op behavior.
2. Implement source index publisher and immutable portable source manifests. Reuse stable per-project source root/path/content identity; commit-to-version provenance belongs in manifest. Explicitly label lexical coverage/limitations; preserve binary versions without pretending they are semantic indexes.
3. Integrate refresh outside-lock capture and inside-lock source-revision CAS, separate evidence state, partial/failure handling and committed-head reuse. Dirty-only changes update source observation but do not mint committed versions.
4. Verify parallel clone publication doesn't conflict on local first-observed metadata. Preserve both raw incoming snapshots/history; any metadata equivalence must be restricted to generated source profiles and cannot ignore source content/provenance/actor changes.
5. Verify portable manifest integrity, durable publication, bounds, restart/recovery, authenticated owner-only filesystem access, legacy migration and full relevant suites; bounded independent review before publishing.

No goal or parent gate is complete merely because capture/publisher unit tests pass. Full source integration and final delivery gates remain required.

## Integration rulings

Git path identity is bytes, not assumed UTF-8. Capture retains byte-hex sidecars;
portable logical paths reversibly encode reserved or non-UTF-8 bytes. Only detail
reads decode display text. Watcher diagnostics preserve undecodable byte identity
and mark worktree availability incomplete while committed capture proceeds.
Overlong lexical identifiers/scope paths are omitted with explicit coverage;
committed source bytes are retained. Automatic source profiles reserve ID prefixes
and require complete manifest provenance on import. Their installation-local
first-seen times may coalesce, while raw incoming snapshots and every semantic/
creator/source field remain unchanged or explicitly conflicting. No wall-clock
value chooses a source fact.
