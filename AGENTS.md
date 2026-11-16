# Collaboration Workbench agent entry point

Read docs/HANDOFF.md first. It contains the current checkpoint, unfinished work, validation commands and next steps. Read only the relevant linked plan rather than reconstructing old chats.

This is a standalone application that connects multiple project Git repositories. It is separate from SSG2. Preserve project-owned tasks, research, provenance and source history. Each connected project needs an agent setup contract; project data must not be pushed into this app repository.

Required modes: optional free self-hosted live collaboration, and Git-only synchronization with batched publishing. Never merge SQLite files, use timestamps as conflict winners, or silently overwrite concurrent work. Preserve alternatives and require explicit reconciliation. Keep databases, private evidence, credentials and bearer tokens out of Git.

Preserve draft/recovery behavior, exact retry bytes, authorization boundaries, unknown metadata, historical citations and explicit acceptance gates. Source observations, derivation assertions and negative searches have bounded meanings; none automatically establishes forensic proof or native parity.

Use the FamilySearch/Puzzilla navigation workflow as inspiration: connected graph/tree, selection inspector, clear lineage and contextual research. Extend existing Evidence navigation rather than duplicating it. The source-criticism UI is unfinished; backend tests do not establish UI completion.

Run task-relevant checks before claims or commits. Do not migrate, move or replace an existing project's authority database without an explicit audited cutover. Coordinate ownership before staging; commit only assigned files. See HANDOFF for this machine's legacy tracker and release gates.
