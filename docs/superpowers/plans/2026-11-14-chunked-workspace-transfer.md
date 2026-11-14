# Chunked workspace transfer implementation plan

> **For agentic workers:** Use scoped subagent-driven implementation and independent review. Source SQLite workspace owns live claims/state/evidence; this file defines interfaces, not task status.

**Goal:** Full default20k-token and declared50k-observation/10k-task data can share through Git and live HTTP without giant wire objects or partial authority.

**Architecture:** Canonical JSON is split into bounded raw-byte pieces with an ordered content-addressed manifest and an aggregate digest. Git stores manifest plus chunk blobs in its private bare-cache data branch; small/legacy workspace.json remains readable. HTTP stages principal/project-bound pieces privately, verifies the complete payload before invoking existing revision-checked domain APIs, and returns bounded chunked responses to opted-in clients. Local live-client state moves large immutable documents into content-addressed private cache files; frozen request IDs/bodies and current authentication checks remain intact. Fix measured/structural quadratic import lookup before full scale apply.

**Tech stack:** Python standard library, existing SQLite/Git/HTTP transaction and authentication boundaries.

**Spec:** prior standalone/hybrid design and user requirements; default-source measure39,843,871 canonical bytes vs current8MiB transfer caps.

## Fixed shared codec interface (worker owns)

File tools/workspace_bundle_chunks.py. Constants FORMAT='workbench-chunked-json', VERSION=1, DEFAULT_CHUNK_BYTES=4*1024*1024, DEFAULT_MAX_BYTES=256*1024*1024, MAX_CHUNKS=1024. Error ChunkError(ValueError). `canonical(payload, max_total_bytes=DEFAULT_MAX_BYTES) -> bytes` requires a JSON object, sorted keys/UTF8/noNaN. `pack(payload, *, chunk_bytes=DEFAULT_CHUNK_BYTES, max_total_bytes=DEFAULT_MAX_BYTES) -> (manifest, chunks_dict)`; chunks_dict maps SHA256 lowercase hex to raw bytes. `validate_manifest(manifest, *, max_total_bytes=DEFAULT_MAX_BYTES, max_chunk_bytes=DEFAULT_CHUNK_BYTES) -> None` checks all exact fields/types/bounds/sums/shape. `unpack(manifest, read_chunk, *, max_total_bytes=DEFAULT_MAX_BYTES, max_chunk_bytes=DEFAULT_CHUNK_BYTES) -> dict` checks every piece and whole digest before parsing/canonical-object validation. Split raw UTF8 bytes; a piece may end inside a codepoint. Repeated identical pieces may reuse one content-addressed blob. Never trust paths from manifest.

Manifest exact shape: {format:FORMAT,version:1,payload_bytes:INT,payload_sha256:HEX64,chunk_bytes:INT,chunks:[{sha256:HEX64,byte_length:INT},...]}. At least one piece; all but last full chunk_bytes; last positive <=chunk_bytes; chunk_bytes positive <= configured per-piece bound; max1024 entries; declared lengths sum exactly payload_bytes. Integers exclude booleans. Aggregate<=256MiB default. Duplicate hash with inconsistent length rejects. Missing/reordered/substituted/truncated pieces or invalid JSON rejects before data authority changes.

Worker also owns only tools/workspace_git_transport.py and tests/test_workspace_bundle_chunks.py + tests/test_workspace_git_transport.py. Keep existing class methods/signatures compatible; max_payload_bytes remains aggregate (default increases to256MiB), max_object_bytes stays9MiB and is independent of aggregate. Small payload roots remain legacy workspace.json up to8MiB/objectbound; large roots use codec manifest plus content-addressed chunk files. Each blob size checked before reading, root manifest/tree validates exact expected chunk paths/types, accepted cache size checked before reading. Preserve no-op, merge callback actual common bases, history rewrite rejection, rejected-push retry and source worktree isolation. No staging/commits while worker ownership active. Coordinator integrates/reviews.

## Coordinator-owned integration

1. Add failing staged HTTP request/recovery/auth tests, then private transfer tables (excluded from portable data). Bind transfer ID+manifest+target to current project/principal. Cap pieces, total bytes, concurrent jobs, cached bytes and expiry; cache writes do not advance domain revision.
2. Complete staging verifies all content before executing original endpoints with fresh token/actor/capability and revision checks. Support transactions/exchange previews and owner local-control calls used by live reconciliation. Never proxy a privileged filesystem credential for a guest.
3. Opted-in bounded HTTP responses cache/export chunk manifests; client decodes verified pieces, acknowledges cleanup, and keeps frozen requests after uncertain commit replies. Legacy small callers remain unchanged.
4. Content-addressed private live-state cache deduplicates large portable bundles/preview documents, keeps the current small state manifest and validates content/binding on restart. No inline32MiB state failure for default38MiB source bundles.
5. Make live client's existing reconciliation state machine use chunk-aware requests; preserve exact request replay and explicit conflict review. Do not silently rebase409.
6. Preindex incoming table rows once per import rather than once per row; profile and verify full-scale apply.
7. Exercise source20k and50k-observation/10k-task fixtures through real Git and authenticated HTTP/live paths, interrupted transfers, tampering, stale revisions, role changes and restart. Run existing full suite and independent review before publishing. Full parent acceptance waits for both transports and scale, not one successful primitive.
