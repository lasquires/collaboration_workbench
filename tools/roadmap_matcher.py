"""Transactional research hint matching, delivery and bounded replay jobs.

All functions operate on the caller's SQLite connection and never commit.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from .roadmap_match_rules import match_question
    from .roadmap_research import canonical, error, scoped
except ImportError:
    from roadmap_match_rules import match_question
    from roadmap_research import canonical, error, scoped


OPS = {"subscribe", "decide_hint", "mark_inbox_read", "replay_rule", "retry_failed_job"}
RESOURCES = {"hints", "subscriptions", "inbox", "processing-status", "replay-jobs"}
RULE_ID = "exact-scoped"
DEFAULT_RULE_VERSION = "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_schema(db: sqlite3.Connection) -> None:
    """Create matcher-owned tables; safe to call after the research schema once."""
    statements = [
        "CREATE TABLE IF NOT EXISTS hints(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,question_id TEXT NOT NULL,criteria_revision INTEGER NOT NULL,candidate_type TEXT NOT NULL,candidate_id TEXT NOT NULL,evidence_revision INTEGER NOT NULL,rule_id TEXT NOT NULL,rule_version TEXT NOT NULL,fingerprint TEXT NOT NULL,predecessor_id TEXT,state TEXT NOT NULL CHECK(state IN ('new','investigating','useful','dismissed','stale')),created_at TEXT NOT NULL,UNIQUE(project_id,id),UNIQUE(project_id,question_id,criteria_revision,candidate_type,candidate_id,evidence_revision,rule_id,rule_version),FOREIGN KEY(project_id,question_id) REFERENCES questions(project_id,id))",
        "CREATE INDEX IF NOT EXISTS hints_semantic ON hints(project_id,question_id,candidate_type,candidate_id,evidence_revision,fingerprint)",
        "CREATE TABLE IF NOT EXISTS hint_presentations(hint_id TEXT NOT NULL,revision INTEGER NOT NULL,reason TEXT NOT NULL,compared_json TEXT NOT NULL,scope_constraints_json TEXT NOT NULL,support_hashes_json TEXT NOT NULL,PRIMARY KEY(hint_id,revision),FOREIGN KEY(hint_id) REFERENCES hints(id))",
        "CREATE TABLE IF NOT EXISTS hint_decisions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,hint_id TEXT NOT NULL,principal_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,recorded_at TEXT NOT NULL,carried_from_id TEXT,request_id TEXT,FOREIGN KEY(project_id,hint_id) REFERENCES hints(project_id,id),FOREIGN KEY(principal_id) REFERENCES principals(id))",
        "CREATE INDEX IF NOT EXISTS hint_decisions_latest ON hint_decisions(project_id,hint_id,principal_id,recorded_at)",
        "CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,principal_id TEXT NOT NULL,target_type TEXT NOT NULL CHECK(target_type IN ('question','task','area')),target_id TEXT NOT NULL,selector_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(project_id,principal_id,target_type,target_id),FOREIGN KEY(project_id,principal_id) REFERENCES project_members(project_id,principal_id))",
        "CREATE TABLE IF NOT EXISTS inbox_items(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,recipient_id TEXT NOT NULL,semantic_identity TEXT NOT NULL,transition_key TEXT NOT NULL,digest_key TEXT NOT NULL,read_at TEXT,created_at TEXT NOT NULL,UNIQUE(project_id,recipient_id,semantic_identity,transition_key),FOREIGN KEY(project_id,recipient_id) REFERENCES project_members(project_id,principal_id))",
        "CREATE TABLE IF NOT EXISTS inbox_members(inbox_id TEXT NOT NULL,hint_id TEXT NOT NULL,PRIMARY KEY(inbox_id,hint_id),FOREIGN KEY(inbox_id) REFERENCES inbox_items(id),FOREIGN KEY(hint_id) REFERENCES hints(id))",
        "CREATE TABLE IF NOT EXISTS replay_jobs(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,rule_id TEXT NOT NULL,rule_version TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','running','complete','failed')),fanout_offset INTEGER NOT NULL DEFAULT 0,request_id TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,last_error TEXT,attempt_count INTEGER NOT NULL DEFAULT 0,next_attempt_at TEXT,UNIQUE(project_id,request_id))",
        "CREATE INDEX IF NOT EXISTS replay_jobs_pending ON replay_jobs(project_id,state,created_at,id)",
    ]
    for statement in statements:
        db.execute(statement)
    columns = {r[1] for r in db.execute("PRAGMA table_info(research_outbox)")}
    if "fanout_offset" not in columns:
        db.execute("ALTER TABLE research_outbox ADD COLUMN fanout_offset INTEGER NOT NULL DEFAULT 0")
    replay_columns = {r[1] for r in db.execute("PRAGMA table_info(replay_jobs)")}
    if "attempt_count" not in replay_columns:
        db.execute("ALTER TABLE replay_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
    if "next_attempt_at" not in replay_columns:
        db.execute("ALTER TABLE replay_jobs ADD COLUMN next_attempt_at TEXT")
    for table in ("hint_decisions",):
        for verb in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'hint decisions are append-only'); END")


def _member(db, project: str, principal: str) -> None:
    if not db.execute("SELECT 1 FROM project_members WHERE project_id=? AND principal_id=?", (project, principal)).fetchone():
        error(403, "Principal lacks project membership")


def _question(db, project: str, question_id: str) -> sqlite3.Row:
    row = db.execute("SELECT q.*,r.text,r.expected_evidence,r.criteria_json,r.criteria_revision,r.presentation_revision FROM questions q JOIN question_revisions r ON r.project_id=q.project_id AND r.id=q.current_revision_id WHERE q.project_id=? AND q.id=?", (project, question_id)).fetchone()
    if row is None:
        error(404, "Scoped question not found")
    return row


def apply_operation(db: sqlite3.Connection, project: str, principal: str, op: dict,
                    request_id: str) -> dict:
    """Apply one matcher-owned operation inside the caller's transaction."""
    if not isinstance(op, dict):
        error(400, "Invalid matcher operation")
    _member(db, project, principal)
    kind = op.get("op")
    now = _now()
    if kind == "subscribe":
        target_type, target_id = op.get("target_type"), op.get("target_id")
        if target_type not in ("question", "task", "area") or not isinstance(target_id, str) or not target_id:
            error(400, "Subscription requires question, task or area target")
        if target_type == "question":
            scoped(db, "questions", project, target_id)
        elif target_type == "area":
            scoped(db, "areas", project, target_id)
        elif not db.execute("SELECT 1 FROM tasks WHERE project_id=? AND uid=?", (project, target_id)).fetchone():
            error(404, "Scoped task not found")
        selector = op.get("selector", {})
        if not isinstance(selector, dict) or selector:
            error(400, "Only exact target-scoped subscriptions are supported")
        row = db.execute("SELECT id FROM subscriptions WHERE project_id=? AND principal_id=? AND target_type=? AND target_id=?", (project, principal, target_type, target_id)).fetchone()
        identity = row["id"] if row else op.get("id", str(uuid.uuid4()))
        if not row:
            db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?)", (identity, project, principal, target_type, target_id, canonical(selector), now))
        return {"type": "subscription", "id": identity}
    if kind == "decide_hint":
        hint = scoped(db, "hints", project, op.get("hint_id"))
        state = op.get("state")
        if state not in ("investigating", "useful", "dismissed"):
            error(400, "Invalid hint decision state")
        reason = op.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 4000 or (state == "dismissed" and not reason.strip()):
            error(400, "Dismissal requires a reason")
        identity = op.get("id", str(uuid.uuid4()))
        db.execute("INSERT INTO hint_decisions VALUES(?,?,?,?,?,?,?,?,?)", (identity, project, hint["id"], principal, state, reason, now, None, request_id))
        db.execute("UPDATE hints SET state=? WHERE project_id=? AND id=?", (state, project, hint["id"]))
        return {"type": "hint_decision", "id": identity, "hint_id": hint["id"], "state": state}
    if kind == "mark_inbox_read":
        inbox_id = op.get("inbox_id")
        row = db.execute("SELECT id FROM inbox_items WHERE project_id=? AND recipient_id=? AND id=?", (project, principal, inbox_id)).fetchone()
        if row is None:
            error(404, "Inbox item not found for principal")
        db.execute("UPDATE inbox_items SET read_at=COALESCE(read_at,?) WHERE id=?", (now, inbox_id))
        return {"type": "inbox_item", "id": inbox_id, "read": True}
    if kind == "replay_rule":
        rule_id, version = op.get("rule_id", RULE_ID), op.get("rule_version")
        if rule_id != RULE_ID or not isinstance(version, str) or not version:
            error(400, "Unsupported replay rule")
        old = db.execute("SELECT * FROM replay_jobs WHERE project_id=? AND request_id=?", (project, request_id)).fetchone()
        if old:
            if (old["rule_id"], old["rule_version"]) != (rule_id, version):
                error(409, "Replay request ID reused with changed content")
            return {"type": "replay_job", "id": old["id"], "state": old["state"]}
        identity = op.get("id", str(uuid.uuid4()))
        db.execute("INSERT INTO replay_jobs(id,project_id,rule_id,rule_version,state,request_id,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?,?)", (identity, project, rule_id, version, request_id, now, now))
        return {"type": "replay_job", "id": identity, "state": "pending"}
    if kind == "retry_failed_job":
        target_type, target_id = op.get("target_type"), op.get("job_id")
        if target_type == "outbox":
            row = db.execute("SELECT id FROM research_outbox WHERE project_id=? AND id=? AND state='failed'", (project, target_id)).fetchone()
            if row is None:
                error(404, "Failed outbox job not found")
            db.execute("UPDATE research_outbox SET state='pending',next_attempt_at=NULL,last_error=NULL WHERE id=?", (target_id,))
        elif target_type == "replay":
            row = db.execute("SELECT id FROM replay_jobs WHERE project_id=? AND id=? AND state='failed'", (project, target_id)).fetchone()
            if row is None:
                error(404, "Failed replay job not found")
            db.execute("UPDATE replay_jobs SET state='pending',next_attempt_at=NULL,last_error=NULL WHERE id=?", (target_id,))
        else:
            error(400, "retry_failed_job requires outbox or replay target_type")
        return {"type": "matcher_job", "id": target_id, "state": "pending"}
    error(400, "Unknown matcher operation")


def _candidate(row: sqlite3.Row) -> dict:
    locator = json.loads(row["locator_key"])
    value = json.loads(row["value_json"])
    candidate = {
        "id": row["id"], "project_id": row["project_id"], "candidate_type": "observation",
        "evidence_revision": row["evidence_revision"], "identifier": row["identifier"],
        "namespace": row["namespace"], "scope_key": row["scope_key"],
        "artifact_version_id": row["artifact_version_id"], "binary_build_id": row["binary_build_id"],
        "locator_key": locator, "support_hashes": [row["source_sha256"], row["content_hash"]],
        "source_run_id": row["source_run_id"],
    }
    value_fields = value if isinstance(value, dict) else {}
    locator_fields = locator if isinstance(locator, dict) else {}
    for field in ("native_mapping_id", "type", "offset"):
        if field in value_fields and isinstance(value_fields[field], (str, int, float)) and not isinstance(value_fields[field], bool):
            candidate[field] = value_fields[field]
        elif field in locator_fields and isinstance(locator_fields[field], (str, int, float)) and not isinstance(locator_fields[field], bool):
            candidate[field] = locator_fields[field]
    return candidate


def _observation_count(db, project: str, *, run_id: str | None = None) -> int:
    sql = """SELECT count(*) FROM observations o WHERE o.project_id=? AND EXISTS (
        SELECT 1 FROM run_observations ro
        JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id
        WHERE ro.project_id=o.project_id AND ro.observation_id=o.id
          AND r.state='published'"""
    args: list[Any] = [project]
    if run_id is not None:
        sql += " AND r.id=?"
        args.append(run_id)
    sql += ")"
    return db.execute(sql, args).fetchone()[0]



def _page_cached(cache: dict | None, key: tuple, load):
    """Reuse read-only source lookups within one durable matcher page."""
    if cache is None:
        return load()
    if key not in cache:
        cache[key] = load()
    return cache[key]


def _observation_at(db, project: str, offset: int, *, run_id: str | None = None) -> sqlite3.Row | None:
    membership = "SELECT MIN(r.id) FROM run_observations ro JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id WHERE ro.project_id=o.project_id AND ro.observation_id=o.id AND r.state='published'"
    args: list[Any] = []
    if run_id is not None:
        membership += " AND r.id=?"
        args.append(run_id)
    run_filter = " AND r.id=?" if run_id is not None else ""
    args = [project]
    if run_id is not None:
        args.append(run_id)
    args.extend([offset, project])
    if run_id is not None:
        args.append(run_id)
    sql = """
        WITH selected AS (
            SELECT o.* FROM observations o
            WHERE o.project_id=? AND EXISTS (
                SELECT 1 FROM run_observations ro
                JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id
                WHERE ro.project_id=o.project_id AND ro.observation_id=o.id
                  AND r.state='published'""" + run_filter + """
            ) ORDER BY o.id LIMIT 1 OFFSET ?
        )
        SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,
               v.binary_build_id,v.sha256 AS source_sha256,
               (SELECT MIN(r.id) FROM run_observations ro
                JOIN index_runs r ON r.project_id=ro.project_id AND r.id=ro.run_id
                WHERE ro.project_id=? AND ro.observation_id=o.id
                  AND r.state='published'""" + run_filter + """) AS source_run_id
        FROM selected o
        JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id
        JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id
    """
    return db.execute(sql, args).fetchone()


_FINDING_SCOPE_SQL = "json_type(r.scope_json,'$.identifier')='text' AND length(json_extract(r.scope_json,'$.identifier'))>0 AND json_type(r.scope_json,'$.namespace')='text' AND length(json_extract(r.scope_json,'$.namespace'))>0 AND json_type(r.scope_json,'$.scope_key')='text' AND length(json_extract(r.scope_json,'$.scope_key'))>0 AND json_type(r.scope_json,'$.artifact_version_id')='text' AND length(json_extract(r.scope_json,'$.artifact_version_id'))>0"


def _finding_count(db: sqlite3.Connection, project: str, *, revision_id: str | None = None) -> int:
    sql = f"SELECT count(*) FROM finding_revisions r WHERE r.project_id=? AND EXISTS(SELECT 1 FROM findings f WHERE f.project_id=r.project_id AND f.archived_at IS NULL AND f.current_revision_id=r.id) AND ({_FINDING_SCOPE_SQL}) AND EXISTS(SELECT 1 FROM evidence_links e WHERE e.project_id=r.project_id AND e.subject_type='finding_revision' AND e.subject_id=COALESCE((SELECT evidence_origin_id FROM finding_revision_lineage l WHERE l.project_id=r.project_id AND l.revision_id=r.id),r.id) AND e.state='available' AND e.artifact_version_id=json_extract(r.scope_json,'$.artifact_version_id'))"
    args: list[Any] = [project]
    if revision_id is not None:
        sql += " AND COALESCE((SELECT evidence_origin_id FROM finding_revision_lineage l WHERE l.project_id=r.project_id AND l.revision_id=r.id),r.id)=?"; args.append(revision_id)
    return db.execute(sql, args).fetchone()[0]


def _finding_at(db: sqlite3.Connection, project: str, offset: int, *, revision_id: str | None = None) -> dict | None:
    sql = f"SELECT r.*,json_extract(r.scope_json,'$.identifier') AS identifier,json_extract(r.scope_json,'$.namespace') AS namespace,json_extract(r.scope_json,'$.scope_key') AS scope_key,json_extract(r.scope_json,'$.artifact_version_id') AS artifact_version_id,v.binary_build_id FROM finding_revisions r JOIN artifact_versions v ON v.project_id=r.project_id AND v.id=json_extract(r.scope_json,'$.artifact_version_id') WHERE r.project_id=? AND EXISTS(SELECT 1 FROM findings f WHERE f.project_id=r.project_id AND f.archived_at IS NULL AND f.current_revision_id=r.id) AND ({_FINDING_SCOPE_SQL}) AND EXISTS(SELECT 1 FROM evidence_links e WHERE e.project_id=r.project_id AND e.subject_type='finding_revision' AND e.subject_id=COALESCE((SELECT evidence_origin_id FROM finding_revision_lineage l WHERE l.project_id=r.project_id AND l.revision_id=r.id),r.id) AND e.state='available' AND e.artifact_version_id=json_extract(r.scope_json,'$.artifact_version_id'))"
    args: list[Any] = [project]
    if revision_id is not None:
        sql += " AND COALESCE((SELECT evidence_origin_id FROM finding_revision_lineage l WHERE l.project_id=r.project_id AND l.revision_id=r.id),r.id)=?"; args.append(revision_id)
    sql += " ORDER BY r.id LIMIT 1 OFFSET ?"; args.append(offset)
    row = db.execute(sql, args).fetchone()
    if row is None:
        return None
    scope = json.loads(row["scope_json"])
    lineage=db.execute("SELECT evidence_origin_id FROM finding_revision_lineage WHERE project_id=? AND revision_id=?",(project,row["id"])).fetchone()
    evidence_origin=lineage[0] if lineage else row["id"]
    links = db.execute("SELECT locator_json,support_hash FROM evidence_links WHERE project_id=? AND subject_type='finding_revision' AND subject_id=? AND artifact_version_id=? AND state='available' ORDER BY id", (project, evidence_origin, row["artifact_version_id"])).fetchall()
    if not links:
        return None
    candidate = {
        "id": evidence_origin, "project_id": project, "candidate_type": "finding_revision",
        "evidence_revision": row["evidence_revision"], "identifier": row["identifier"],
        "namespace": row["namespace"], "scope_key": row["scope_key"],
        "artifact_version_id": row["artifact_version_id"], "binary_build_id": row["binary_build_id"],
        "locator_key": json.loads(links[0]["locator_json"]),
        "support_hashes": sorted({link["support_hash"] for link in links}),
        "source_run_id": "finding:" + evidence_origin,
    }
    for field in ("native_mapping_id", "type", "offset"):
        value = scope.get(field)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            candidate[field] = value
    return candidate


def _candidate_count(db, project: str, *, run_id: str | None = None,
                     finding_revision_id: str | None = None, cache: dict | None = None) -> int:
    if finding_revision_id is not None:
        return _finding_count(db, project, revision_id=finding_revision_id)
    observations = _page_cached(cache, ("observation_count", project, run_id),
        lambda: _observation_count(db, project, run_id=run_id))
    return observations if run_id is not None else observations + _finding_count(db, project)


def _candidate_at(db, project: str, offset: int, *, run_id: str | None = None,
                  finding_revision_id: str | None = None, cache: dict | None = None) -> dict | None:
    key = ("candidate_at", project, offset, run_id, finding_revision_id)
    if cache is not None and key in cache:
        return cache[key]
    if finding_revision_id is not None:
        result = _finding_at(db, project, offset, revision_id=finding_revision_id)
        if cache is not None: cache[key] = result
        return result
    observation_count = _page_cached(cache, ("observation_count", project, run_id),
        lambda: _observation_count(db, project, run_id=run_id))
    if offset < observation_count:
        row = _observation_at(db, project, offset, run_id=run_id)
        result = _candidate(row) if row is not None else None
    elif run_id is not None:
        result = None
    else:
        result = _finding_at(db, project, offset - observation_count)
    if cache is not None: cache[key] = result
    return result


def _stale_unavailable_findings(db: sqlite3.Connection, project: str) -> int:
    changed=db.execute("""UPDATE hints AS h SET state='stale'
        WHERE h.project_id=? AND h.candidate_type='finding_revision' AND h.state<>'stale'
        AND (NOT EXISTS(SELECT 1 FROM findings f WHERE f.project_id=h.project_id AND f.archived_at IS NULL AND COALESCE((SELECT evidence_origin_id FROM finding_revision_lineage l WHERE l.project_id=f.project_id AND l.revision_id=f.current_revision_id),f.current_revision_id)=h.candidate_id) OR NOT EXISTS(SELECT 1 FROM evidence_links e JOIN finding_revisions r
                ON r.project_id=e.project_id AND r.id=e.subject_id
                WHERE e.project_id=h.project_id AND e.subject_type='finding_revision'
                AND e.subject_id=h.candidate_id AND e.state='available'
                AND e.artifact_version_id=json_extract(r.scope_json,'$.artifact_version_id'))
            OR EXISTS(SELECT 1 FROM hint_presentations p,json_each(p.support_hashes_json) support
                WHERE p.hint_id=h.id AND p.revision=(SELECT MAX(p2.revision) FROM hint_presentations p2 WHERE p2.hint_id=h.id)
                AND NOT EXISTS(SELECT 1 FROM evidence_links e JOIN finding_revisions r
                    ON r.project_id=e.project_id AND r.id=e.subject_id
                    WHERE e.project_id=h.project_id AND e.subject_type='finding_revision'
                    AND e.subject_id=h.candidate_id AND e.state='available'
                    AND e.artifact_version_id=json_extract(r.scope_json,'$.artifact_version_id')
                    AND e.support_hash=support.value))) RETURNING id,question_id""",(project,)).fetchall()
    for hint in changed:
        recipients={row[0] for row in db.execute("SELECT DISTINCT d.principal_id FROM hint_decisions d JOIN project_members m ON m.project_id=d.project_id AND m.principal_id=d.principal_id WHERE d.project_id=? AND d.hint_id=? AND d.state='useful'",(project,hint['id']))}
        for recipient in recipients:
            semantic='evidence-review:'+hint['id']
            inbox_id=str(uuid.uuid5(uuid.NAMESPACE_URL,project+':'+recipient+':'+semantic))
            db.execute("INSERT OR IGNORE INTO inbox_items VALUES(?,?,?,?,?,?,NULL,?)",(inbox_id,project,recipient,semantic,'evidence-review',hint['question_id'],_now()))
            db.execute('INSERT OR IGNORE INTO inbox_members VALUES(?,?)',(inbox_id,hint['id']))
    return len(changed)


def _question_count(db, project: str, *, question_id: str | None = None,
                    cache: dict | None = None) -> int:
    sql = "SELECT count(*) FROM questions q JOIN question_revisions r ON r.project_id=q.project_id AND r.id=q.current_revision_id WHERE q.project_id=? AND q.state IN ('open','investigating') AND r.criteria_json NOT IN ('{}','null','[]')"
    args: list[Any] = [project]
    if question_id is not None:
        sql += " AND q.id=?"; args.append(question_id)
    return _page_cached(cache, ("question_count", project, question_id),
        lambda: db.execute(sql, args).fetchone()[0])


def _question_at(db, project: str, offset: int, *, question_id: str | None = None,
                 cache: dict | None = None) -> sqlite3.Row | None:
    sql = "SELECT q.*,r.text,r.expected_evidence,r.criteria_json,r.criteria_revision,r.presentation_revision FROM questions q JOIN question_revisions r ON r.project_id=q.project_id AND r.id=q.current_revision_id WHERE q.project_id=? AND q.state IN ('open','investigating') AND r.criteria_json NOT IN ('{}','null','[]')"
    args: list[Any] = [project]
    if question_id is not None:
        sql += " AND q.id=?"; args.append(question_id)
    sql += " ORDER BY q.id LIMIT 1 OFFSET ?"; args.append(offset)
    return _page_cached(cache, ("question_at", project, offset, question_id),
        lambda: db.execute(sql, args).fetchone())


def _recipients(db, project: str, question: sqlite3.Row) -> set[str]:
    result = {r[0] for r in db.execute("SELECT principal_id FROM subscriptions WHERE project_id=? AND target_type='question' AND target_id=?", (project, question["id"]))}
    if question["area_id"]:
        result.update(r[0] for r in db.execute("SELECT principal_id FROM subscriptions WHERE project_id=? AND target_type='area' AND target_id=?", (project, question["area_id"])))
    if question["owner_task_id"]:
        result.update(r[0] for r in db.execute("SELECT principal_id FROM subscriptions WHERE project_id=? AND target_type='task' AND target_id=?", (project, question["owner_task_id"])))
        task = db.execute("SELECT record_json FROM tasks WHERE project_id=? AND uid=?", (project, question["owner_task_id"])).fetchone()
        if task:
            record = json.loads(task["record_json"])
            labels = []
            for field in ("owner", "claim"):
                value = record.get(field)
                if isinstance(value, str) and value.strip():
                    labels.append(value.strip())
                elif isinstance(value, dict):
                    label = value.get("by") or value.get("label")
                    if isinstance(label, str) and label.strip():
                        labels.append(label.strip())
            for label in labels:
                result.update(r[0] for r in db.execute("SELECT p.id FROM principals p JOIN project_members m ON m.principal_id=p.id WHERE m.project_id=? AND p.label=?", (project, label)))
    return {p for p in result if db.execute("SELECT 1 FROM project_members WHERE project_id=? AND principal_id=?", (project, p)).fetchone()}


def _store_match(db: sqlite3.Connection, project: str, question: sqlite3.Row,
                 candidate: dict, rule_version: str) -> bool:
    criteria = json.loads(question["criteria_json"])
    qrev = {"id": question["id"], "project_id": project,
            "criteria_revision": question["criteria_revision"], "criteria": criteria}
    match = match_question(qrev, candidate, {"id": RULE_ID, "version": rule_version})
    if match is None:
        return False
    old = db.execute("SELECT * FROM hints WHERE project_id=? AND question_id=? AND criteria_revision=? AND candidate_type=? AND candidate_id=? AND evidence_revision=? AND rule_id=? AND rule_version=?", (project, question["id"], question["criteria_revision"], candidate["candidate_type"], candidate["id"], candidate["evidence_revision"], RULE_ID, rule_version)).fetchone()
    if old:
        return False
    prior = db.execute("SELECT * FROM hints WHERE project_id=? AND question_id=? AND candidate_type=? AND candidate_id=? AND fingerprint=? ORDER BY created_at DESC,id DESC LIMIT 1", (project, question["id"], candidate["candidate_type"], candidate["id"], match["fingerprint"])).fetchone()
    predecessor = db.execute("SELECT id FROM hints WHERE project_id=? AND question_id=? AND candidate_type=? AND candidate_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (project, question["id"], candidate["candidate_type"], candidate["id"])).fetchone()
    if candidate["candidate_type"] == "finding_revision":
        previous_hints = db.execute("SELECT h.id,p.support_hashes_json FROM hints h JOIN hint_presentations p ON p.hint_id=h.id AND p.revision=(SELECT max(p2.revision) FROM hint_presentations p2 WHERE p2.hint_id=h.id) WHERE h.project_id=? AND h.question_id=? AND h.candidate_type='finding_revision' AND h.candidate_id=?", (project, question["id"], candidate["id"])).fetchall()
        current_support = set(match["support_hashes"])
        for previous in previous_hints:
            if set(json.loads(previous["support_hashes_json"])) - current_support:
                db.execute("UPDATE hints SET state='stale' WHERE project_id=? AND id=?", (project, previous["id"]))
    hint_id = str(uuid.uuid4())
    db.execute("INSERT INTO hints VALUES(?,?,?,?,?,?,?,?,?,?,?,'new',?)", (hint_id, project, question["id"], question["criteria_revision"], candidate["candidate_type"], candidate["id"], candidate["evidence_revision"], RULE_ID, rule_version, match["fingerprint"], predecessor["id"] if predecessor else None, _now()))
    db.execute("INSERT INTO hint_presentations VALUES(?,?,?,?,?,?)", (hint_id, question["presentation_revision"], match["reason"], canonical(match["compared_fields"]), canonical(match["scope_constraints"]), canonical(match["support_hashes"])))
    carried = False
    if prior:
        decision = db.execute("SELECT * FROM hint_decisions WHERE project_id=? AND hint_id=? ORDER BY recorded_at DESC,id DESC LIMIT 1", (project, prior["id"])).fetchone()
        if decision:
            db.execute("INSERT INTO hint_decisions VALUES(?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), project, hint_id, decision["principal_id"], decision["state"], decision["reason"], _now(), decision["id"], None))
            db.execute("UPDATE hints SET state=? WHERE id=? AND project_id=?", (decision["state"], hint_id, project))
            carried = True
    if not carried:
        for recipient in _recipients(db, project, question):
            semantic = f"{question['id']}:{candidate['source_run_id']}"
            digest_key = semantic
            inbox = db.execute("SELECT id FROM inbox_items WHERE project_id=? AND recipient_id=? AND semantic_identity=? AND transition_key='new-match'", (project, recipient, semantic)).fetchone()
            if inbox:
                inbox_id = inbox["id"]
            else:
                inbox_id = str(uuid.uuid4())
                db.execute("INSERT INTO inbox_items VALUES(?,?,?,?,?,?,NULL,?)", (inbox_id, project, recipient, semantic, "new-match", digest_key, _now()))
            db.execute("INSERT OR IGNORE INTO inbox_members VALUES(?,?)", (inbox_id, hint_id))
    return True


def _process_pair(db, project: str, question: sqlite3.Row, candidate_row: sqlite3.Row | dict,
                  rule_version: str) -> bool:
    candidate = candidate_row if isinstance(candidate_row, dict) else _candidate(candidate_row)
    try:
        return _store_match(db, project, question, candidate, rule_version)
    except ValueError as exc:
        raise ValueError(f"question {question['id']}: {exc}") from exc


def _question_candidates(db, project: str, identifiers: list[str], offset: int,
                         limit: int) -> tuple[list[tuple[int, Any]], int]:
    """Identifier-prefiltered page retaining the original global ordinal space.

    Rank published observations once, rather than running a correlated OFFSET
    lookup for each observation. Findings keep their existing evidence gate.
    Scope/build/type matching still belongs to _process_pair, never this filter.
    """
    observations = _observation_count(db, project)
    total = observations + _finding_count(db, project)
    marks = ','.join('?' for _ in identifiers)
    candidates = []
    if offset < observations:
        rows = db.execute(f"""
            WITH published AS (
                SELECT ro.observation_id,MIN(r.id) AS source_run_id
                FROM run_observations ro JOIN index_runs r
                  ON r.project_id=ro.project_id AND r.id=ro.run_id
                WHERE ro.project_id=? AND r.state='published'
                GROUP BY ro.observation_id
            ), ranked AS (
                SELECT o.*,p.source_run_id,
                       ROW_NUMBER() OVER (ORDER BY o.id)-1 AS candidate_ordinal
                FROM observations o JOIN published p ON p.observation_id=o.id
                WHERE o.project_id=?
            )
            SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,
                   v.binary_build_id,v.sha256 AS source_sha256
            FROM ranked o
            JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id
            JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id
            WHERE o.candidate_ordinal>=? AND o.identifier IN ({marks})
            ORDER BY o.candidate_ordinal LIMIT ?""",
            (project, project, offset, *identifiers, limit)).fetchall()
        candidates.extend((row['candidate_ordinal'], row) for row in rows)
    if len(candidates) < limit:
        # Same current/available-source eligibility and ordering as _finding_at.
        rows = db.execute(f"""
            WITH ranked AS (
                SELECT r.id,json_extract(r.scope_json,'$.identifier') AS identifier,
                       ROW_NUMBER() OVER (ORDER BY r.id)-1 AS candidate_ordinal
                FROM finding_revisions r
                WHERE r.project_id=?
                  AND EXISTS(SELECT 1 FROM findings f WHERE f.project_id=r.project_id
                    AND f.archived_at IS NULL AND f.current_revision_id=r.id)
                  AND ({_FINDING_SCOPE_SQL})
                  AND EXISTS(SELECT 1 FROM evidence_links e
                    WHERE e.project_id=r.project_id AND e.subject_type='finding_revision'
                      AND e.subject_id=COALESCE((SELECT evidence_origin_id
                        FROM finding_revision_lineage l WHERE l.project_id=r.project_id
                          AND l.revision_id=r.id),r.id)
                      AND e.state='available'
                      AND e.artifact_version_id=json_extract(r.scope_json,'$.artifact_version_id'))
            )
            SELECT candidate_ordinal FROM ranked
            WHERE candidate_ordinal>=? AND identifier IN ({marks})
            ORDER BY candidate_ordinal LIMIT ?""",
            (project, max(0, offset-observations), *identifiers, limit-len(candidates))).fetchall()
        for row in rows:
            ordinal = row['candidate_ordinal']
            candidates.append((observations+ordinal, _finding_at(db, project, ordinal)))
    return candidates, total


def _process_event_page(db, project: str, event: sqlite3.Row, remaining: int) -> tuple[int, int, bool]:
    page_cache: dict = {}
    payload = json.loads(event["payload_json"])
    question_id = payload.get("id") if event["kind"] in ("create_question", "revise_question") else None
    run_id = None
    finding_revision_id = None
    if event["kind"] == "publish_run":
        run_id = payload.get("id")
    elif event["kind"] == "publish_finding":
        finding_revision_id = payload.get("id")
    elif event["kind"] not in ("create_question", "revise_question"):
        db.execute("UPDATE research_outbox SET state='done',attempt_count=attempt_count+1,last_error=NULL WHERE id=?", (event["id"],))
        return 0, 0, True
    qcount = _question_count(db, project, question_id=question_id, cache=page_cache)
    if run_id:
        # The run membership primary key already gives unique observations in
        # the same order as the legacy cursor. Fetch a page once per question.
        size = db.execute("""SELECT COUNT(*) FROM run_observations ro JOIN index_runs r
            ON r.project_id=ro.project_id AND r.id=ro.run_id
            WHERE ro.project_id=? AND ro.run_id=? AND r.state='published'""",
            (project, run_id)).fetchone()[0]
        offset = event["fanout_offset"]
        total = size * qcount
        processed = matches = 0
        while offset < total and processed < remaining:
            qi, ci = divmod(offset, size)
            question = _question_at(db, project, qi)
            identifiers = sorted({group['identifier'] for group in json.loads(question['criteria_json'])['any_of']})
            marks = ','.join('?' for _ in identifiers)
            # Exact identifier equality is required by every supported group.
            # Skip an empty question/run intersection without changing the
            # legacy question-major cursor or any nonempty match evaluation.
            exists = db.execute(f"""SELECT 1 FROM observations o JOIN run_observations ro
                ON ro.project_id=o.project_id AND ro.observation_id=o.id
                WHERE ro.project_id=? AND ro.run_id=? AND o.identifier IN ({marks}) LIMIT 1""",
                (project, run_id, *identifiers)).fetchone()
            if exists is None:
                offset = (qi + 1) * size
                continue
            start = db.execute("""SELECT observation_id FROM run_observations
                WHERE project_id=? AND run_id=? ORDER BY observation_id LIMIT 1 OFFSET ?""",
                (project, run_id, ci)).fetchone()
            if start is None:
                raise ValueError("Published run cursor did not resolve to membership")
            budget = remaining - processed
            rows = db.execute(f"""SELECT o.*,c.artifact_version_id,c.namespace,c.scope_key,c.locator_key,
                    v.binary_build_id,v.sha256 AS source_sha256,ro.run_id AS source_run_id
                FROM run_observations ro JOIN observations o ON o.project_id=ro.project_id AND o.id=ro.observation_id
                JOIN occurrences c ON c.project_id=o.project_id AND c.id=o.occurrence_id
                JOIN artifact_versions v ON v.project_id=c.project_id AND v.id=c.artifact_version_id
                WHERE ro.project_id=? AND ro.run_id=? AND ro.observation_id>=?
                AND o.identifier IN ({marks}) ORDER BY ro.observation_id LIMIT ?""",
                (project, run_id, start[0], *identifiers, budget)).fetchall()
            if not rows:
                offset = (qi + 1) * size
                continue
            matches += sum(_process_pair(db, project, question, row, DEFAULT_RULE_VERSION) for row in rows)
            # Keep the persisted cursor in the original full-membership ordinal
            # space, including jobs begun before identifier-prefiltered paging.
            rank = db.execute("""SELECT COUNT(*) FROM run_observations
                WHERE project_id=? AND run_id=? AND observation_id<=?""",
                (project, run_id, rows[-1]['id'])).fetchone()[0]
            offset = (qi + 1) * size if len(rows) < budget else qi * size + rank
            processed += len(rows)
        done = offset >= total
        db.execute("UPDATE research_outbox SET fanout_offset=?,state=?,attempt_count=attempt_count+1,last_error=NULL WHERE id=?",
                   (offset, "done" if done else "pending", event["id"]))
        return processed, matches, done
    if question_id and qcount:
        question = _question_at(db, project, 0, question_id=question_id, cache=page_cache)
        identifiers = sorted({group['identifier'] for group in json.loads(question['criteria_json'])['any_of']})
        # One extra matching identifier tells us whether another page is needed.
        # Persist the legacy observation-then-finding ordinal, including jobs
        # begun before sparse question paging. No full-index per-row fallback.
        candidates, total = _question_candidates(db, project, identifiers, 0, remaining+1)
        # Historical small sparse watches restarted from the matching set and
        # relied on hint identity deduplication. Retain that recovery contract.
        # Larger sets must resume their durable full-index ordinal instead.
        if len(candidates) > remaining and event['fanout_offset']:
            candidates, total = _question_candidates(db, project, identifiers,
                                                      event['fanout_offset'], remaining+1)
        page = candidates[:remaining]
        matches = sum(_process_pair(db, project, question, candidate, DEFAULT_RULE_VERSION)
                      for _, candidate in page if candidate is not None)
        done = len(candidates) <= remaining
        offset = total if done else page[-1][0]+1
        db.execute("UPDATE research_outbox SET fanout_offset=?,state=?,attempt_count=attempt_count+1,last_error=NULL WHERE id=?",
                   (offset, 'done' if done else 'pending', event['id']))
        return len(page), matches, done
    ccount = _candidate_count(db, project, run_id=run_id,
                              finding_revision_id=finding_revision_id, cache=page_cache)
    total = qcount * ccount
    offset = event["fanout_offset"]
    processed = matches = 0
    while offset < total and processed < remaining:
        qi, ci = divmod(offset, max(ccount, 1))
        question = _question_at(db, project, qi, question_id=question_id, cache=page_cache)
        candidate = _candidate_at(db, project, ci, run_id=run_id,
                                  finding_revision_id=finding_revision_id, cache=page_cache)
        if question is None or candidate is None:
            break
        matches += int(_process_pair(db, project, question, candidate, DEFAULT_RULE_VERSION))
        offset += 1; processed += 1
    done = offset >= total
    db.execute("UPDATE research_outbox SET fanout_offset=?,state=?,attempt_count=attempt_count+1,last_error=NULL WHERE id=?", (offset, "done" if done else "pending", event["id"]))
    return processed, matches, done


def _process_replay_page(db, project: str, job: sqlite3.Row, remaining: int) -> tuple[int, int, bool]:
    page_cache: dict = {}
    qcount = _question_count(db, project, cache=page_cache)
    ccount = _candidate_count(db, project, cache=page_cache)
    total = qcount * ccount
    offset = job["fanout_offset"]
    processed = matches = 0
    while offset < total and processed < remaining:
        qi, ci = divmod(offset, max(ccount, 1))
        question = _question_at(db, project, qi, cache=page_cache)
        candidate = _candidate_at(db, project, ci, cache=page_cache)
        if question is None or candidate is None:
            break
        matches += int(_process_pair(db, project, question, candidate, job["rule_version"]))
        offset += 1; processed += 1
    done = offset >= total
    db.execute("UPDATE replay_jobs SET fanout_offset=?,state=?,updated_at=?,last_error=NULL WHERE id=?", (offset, "complete" if done else "running", _now(), job["id"]))
    return processed, matches, done


def process_jobs(db: sqlite3.Connection, project: str, limit: int = 100) -> dict:
    """Process at most `limit` candidate/question pairs, preserving durable cursors."""
    if type(limit) is not int or not 1 <= limit <= 10000:
        error(400, "Job limit must be 1–10000")
    processed = matched = completed = failed = 0
    stale_hints = _stale_unavailable_findings(db, project)
    while processed < limit:
        now = _now()
        event = db.execute("SELECT * FROM research_outbox WHERE project_id=? AND state='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY attempt_count,event_sequence,id LIMIT 1", (project, now)).fetchone()
        if event is None:
            break
        db.execute("SAVEPOINT matcher_outbox_page")
        try:
            count, hits, done = _process_event_page(db, project, event, limit - processed)
        except Exception as exc:
            db.execute("ROLLBACK TO matcher_outbox_page")
            db.execute("RELEASE matcher_outbox_page")
            attempts = event["attempt_count"] + 1
            terminal = isinstance(exc, ValueError) or attempts >= 5
            delay = min(3600, 2 ** min(attempts, 10))
            next_at = None if terminal else (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            db.execute("UPDATE research_outbox SET state=?,attempt_count=?,last_error=?,next_attempt_at=? WHERE id=?", ("failed" if terminal else "pending", attempts, str(exc)[:2000], next_at, event["id"]))
            failed += 1
            continue
        db.execute("RELEASE matcher_outbox_page")
        processed += count; matched += hits
        if done:
            completed += 1
        if count == 0 and not done:
            break
    while processed < limit:
        now = _now()
        job = db.execute("SELECT * FROM replay_jobs WHERE project_id=? AND state IN ('pending','running') AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at,id LIMIT 1", (project, now)).fetchone()
        if job is None:
            break
        db.execute("SAVEPOINT matcher_replay_page")
        try:
            count, hits, done = _process_replay_page(db, project, job, limit - processed)
        except Exception as exc:
            db.execute("ROLLBACK TO matcher_replay_page")
            db.execute("RELEASE matcher_replay_page")
            attempts = job["attempt_count"] + 1
            terminal = isinstance(exc, ValueError) or attempts >= 5
            delay = min(3600, 2 ** min(attempts, 10))
            next_at = None if terminal else (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            db.execute("UPDATE replay_jobs SET state=?,attempt_count=?,next_attempt_at=?,updated_at=?,last_error=? WHERE id=?", ("failed" if terminal else "pending", attempts, next_at, _now(), str(exc)[:2000], job["id"]))
            failed += 1
            continue
        db.execute("RELEASE matcher_replay_page")
        processed += count; matched += hits
        if done:
            completed += 1
        if count == 0 and not done:
            break
    pending = db.execute("SELECT count(*) FROM research_outbox WHERE project_id=? AND state='pending'", (project,)).fetchone()[0]
    replay_pending = db.execute("SELECT count(*) FROM replay_jobs WHERE project_id=? AND state IN ('pending','running')", (project,)).fetchone()[0]
    return {"processed_pairs": processed, "new_hints": matched, "completed_jobs": completed,
            "failed_jobs": failed, "pending_outbox": pending, "pending_replays": replay_pending,
            "stale_hints": stale_hints}


def read(db: sqlite3.Connection, resource: str, project: str, *, limit: int | None = None,
         cursor: str | None = None, principal_id: str | None = None, **unused) -> dict:
    """Read bounded matcher projections under the shared research response envelope."""
    try:
        from .roadmap_projection import API_VERSION, _coverage, _encode, _decode
    except ImportError:
        from roadmap_projection import API_VERSION, _coverage, _encode, _decode
    if resource not in RESOURCES:
        error(404, "Unknown matcher resource")
    if resource in ("inbox", "subscriptions") and principal_id is not None:
        _member(db, project, principal_id)
    limit = 100 if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= 100:
        error(400, "Page limit must be 1–100")
    meta = db.execute("SELECT * FROM store_meta WHERE singleton=1").fetchone()
    revision = f"{meta['store_id']}:{meta['revision']}"
    binding = {"resource": resource, "project_id": project, "revision": revision,
               "limit": limit, "principal_id": principal_id}
    offset = 0
    if cursor:
        page = _decode(cursor)
        if page.get("revision") != revision:
            error(409, "Snapshot cursor expired; restart required")
        if set(page) != set(binding) | {"offset"} or any(page.get(k) != v for k, v in binding.items()) or type(page.get("offset")) is not int or page["offset"] < 0:
            error(400, "Cursor does not match project/filter")
        offset = page["offset"]
    params: list[Any] = [project]
    if resource == "hints":
        query = "SELECT h.*,p.reason,p.compared_json,p.scope_constraints_json,p.support_hashes_json FROM hints h LEFT JOIN hint_presentations p ON p.hint_id=h.id AND p.revision=(SELECT max(p2.revision) FROM hint_presentations p2 WHERE p2.hint_id=h.id) WHERE h.project_id=? ORDER BY h.created_at,h.id LIMIT ? OFFSET ?"
    elif resource == "subscriptions":
        query = "SELECT * FROM subscriptions WHERE project_id=?"
        if principal_id is not None:
            query += " AND principal_id=?"; params.append(principal_id)
        query += " ORDER BY created_at,id LIMIT ? OFFSET ?"
    elif resource == "inbox":
        if principal_id is None:
            error(400, "principal_id is required for inbox reads")
        query = "SELECT * FROM inbox_items WHERE project_id=? AND recipient_id=? ORDER BY created_at,id LIMIT ? OFFSET ?"
        params.append(principal_id)
    elif resource == "replay-jobs":
        query = "SELECT * FROM replay_jobs WHERE project_id=? ORDER BY created_at,id LIMIT ? OFFSET ?"
    else:
        counts = {"pending_outbox": db.execute("SELECT count(*) FROM research_outbox WHERE project_id=? AND state='pending'", (project,)).fetchone()[0],
                  "failed_outbox": db.execute("SELECT count(*) FROM research_outbox WHERE project_id=? AND state='failed'", (project,)).fetchone()[0],
                  "pending_replays": db.execute("SELECT count(*) FROM replay_jobs WHERE project_id=? AND state IN ('pending','running')", (project,)).fetchone()[0],
                  "failed_replays": db.execute("SELECT count(*) FROM replay_jobs WHERE project_id=? AND state='failed'", (project,)).fetchone()[0]}
        counts["errors"] = [dict(r) for r in db.execute("SELECT id,kind,last_error,attempt_count FROM research_outbox WHERE project_id=? AND state='failed' ORDER BY event_sequence DESC LIMIT 20", (project,))]
        counts["errors"].extend(dict(r) for r in db.execute("SELECT id,rule_id,last_error FROM replay_jobs WHERE project_id=? AND state='failed' ORDER BY updated_at DESC LIMIT 20", (project,)))
        # A pending count alone cannot distinguish a stalled job from a large
        # fanout that is advancing. Expose durable cursors without source payloads.
        counts["pending_jobs"] = [dict(r) for r in db.execute(
            "SELECT id,kind,event_sequence,fanout_offset,attempt_count FROM research_outbox "
            "WHERE project_id=? AND state='pending' ORDER BY event_sequence,id LIMIT 20", (project,))]
        counts["pending_jobs_truncated"] = counts["pending_outbox"] > len(counts["pending_jobs"])
        return {"api_version": API_VERSION, "project_id": project, "revision": revision,
                "items": [counts], "coverage": _coverage(db, project), "next_cursor": None}
    params.extend([limit + 1, offset])
    rows = db.execute(query, params).fetchall()
    items = []
    for row in rows[:limit]:
        item = dict(row)
        for key in list(item):
            if key.endswith("_json"):
                item[key[:-5]] = json.loads(item.pop(key))
        if resource == "inbox":
            item["hint_ids"] = [r[0] for r in db.execute("SELECT hint_id FROM inbox_members WHERE inbox_id=? ORDER BY hint_id", (item["id"],))]
        if resource == "hints":
            item["decisions"] = [dict(d) for d in db.execute("SELECT id,principal_id,state,reason,recorded_at,carried_from_id FROM hint_decisions WHERE project_id=? AND hint_id=? ORDER BY recorded_at,id", (project, item["id"]))]
        items.append(item)
    return {"api_version": API_VERSION, "project_id": project, "revision": revision,
            "items": items, "coverage": _coverage(db, project),
            "next_cursor": _encode({**binding, "offset": offset + limit}) if len(rows) > limit else None}
