"""Search indexed Workbench nodes for variable/name instances and provenance."""
import base64
import hashlib
import json
import re


def _cursor(query, key):
    body = json.dumps({"q": query, "after": key}, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def _decode(cursor, query):
    if not cursor:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if value.get("q") != query or not isinstance(value.get("after"), list):
            raise ValueError
        return tuple(value["after"])
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("Invalid variable lookup cursor")


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _norm(value):
    return value.casefold().replace("_", "")


def _matches_name(name, query):
    return query.casefold() in name.casefold() or _norm(query) in _norm(name)


def _task_label(db, task_id):
    if not task_id:
        return None
    row = db.execute("SELECT record_json FROM tasks WHERE uid=?", (task_id,)).fetchone()
    return {"task_uuid": task_id, "title": json.loads(row[0]).get("title", "") } if row else {"task_uuid": task_id, "title": ""}


def lookup_variable_nodes(store, project, query, *, limit=50, cursor=None):
    """Return stable, paged exact and substring candidate hits from one project.

    Findings and observations are indexed evidence nodes. Task titles/journals
    are returned as labeled mentions. A result is one node UUID per evidence hit.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must be 1..500 characters")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    q = query.strip().casefold()
    after = _decode(cursor, q)
    exact, similar = [], []
    with store._connection() as db:
        # Authored findings: evidence-link UUID is the evidence-hit identity;
        # a finding without a source link is retained as an unlinked finding.
        rows = db.execute("""SELECT f.id AS finding_id,r.claim,r.scope_json,r.classification,r.uncertainty,
            r.id AS revision_id,e.id AS evidence_id,e.locator_json,e.method,e.limitations,
            av.id AS version_id,av.sha256,a.relative_path,
            json_extract(r.scope_json,'$.task_id') AS task_id
            FROM findings f JOIN finding_revisions r ON r.finding_id=f.id AND r.project_id=f.project_id
            LEFT JOIN evidence_links e ON e.subject_type='finding_revision' AND e.subject_id=r.id
              AND e.project_id=r.project_id AND e.state='available'
            LEFT JOIN artifact_versions av ON av.id=e.artifact_version_id AND av.project_id=e.project_id
            LEFT JOIN artifacts a ON a.id=av.artifact_id AND a.project_id=av.project_id
            WHERE f.project_id=? ORDER BY r.id,e.id""", (project,)).fetchall()
        for row in rows:
            fields = [row['claim'], row['scope_json'], row['relative_path'] or '', row['locator_json'] or '']
            names = sorted({m for s in fields for m in re.findall(r"[A-Za-z_$][\w$.:/-]*", s) if _matches_name(m, q)})
            if not names and q not in " ".join(fields).casefold():
                continue
            evidence_id = row['evidence_id'] or row['revision_id']
            item = {"node_uuid": row['finding_id'], "node_type": "finding", "evidence_uuid": evidence_id,
                "revision_uuid": row['revision_id'], "match_names": names,
                "claim": row['claim'], "scope": json.loads(row['scope_json']),
                "classification": row['classification'], "uncertainty": row['uncertainty'],
                "task_uuid": row['task_id'], "source_locator": ({"path": row['relative_path'],
                "locator": json.loads(row['locator_json']) if row['locator_json'] else None,
                "artifact_version_uuid": row['version_id'], "sha256": row['sha256'],
                "method": row['method'], "limitations": row['limitations']} if row['evidence_id'] else None),
                "task_title": _task_label(db, row['task_id'])['title'] if row['task_id'] and _task_label(db, row['task_id']) else None,
                "linked_task_labels": [_task_label(db, row['task_id'])] if row['task_id'] and _task_label(db, row['task_id']) else [],
                "provenance": "authored finding; support remains subject to its evidence state and stated scope"}
            (exact if any(n.casefold() == q for n in names) else similar).append(item)

        # Extracted observation occurrences, including ones without a task or finding link.
        rows = db.execute("""SELECT o.id AS observation_id,o.identifier,substr(o.value_json,1,4096) AS value_prefix,
            substr(o.provenance_json,1,4096) AS provenance_prefix,
            oc.id AS occurrence_id,oc.namespace,oc.scope_key,oc.locator_key,av.id AS version_id,av.sha256,
            a.relative_path,(SELECT group_concat(DISTINCT json_extract(fr.scope_json,'$.task_id'))
              FROM finding_revisions fr WHERE fr.project_id=o.project_id
              AND json_extract(fr.scope_json,'$.occurrence_id')=oc.id
              AND json_extract(fr.scope_json,'$.task_id') IS NOT NULL) AS linked_tasks
            FROM observations o JOIN occurrences oc ON oc.id=o.occurrence_id AND oc.project_id=o.project_id
            JOIN artifact_versions av ON av.id=oc.artifact_version_id AND av.project_id=oc.project_id
            JOIN artifacts a ON a.id=av.artifact_id AND a.project_id=av.project_id
            WHERE o.project_id=? AND (o.identifier LIKE ? ESCAPE '\\' COLLATE NOCASE
              OR replace(lower(o.identifier),'_','') LIKE ? ESCAPE '\\'
              OR substr(o.value_json,1,4096) LIKE ? ESCAPE '\\' COLLATE NOCASE
              OR replace(lower(substr(o.value_json,1,4096)),'_','') LIKE ? ESCAPE '\\'
              OR substr(o.provenance_json,1,4096) LIKE ? ESCAPE '\\' COLLATE NOCASE
              OR replace(lower(substr(o.provenance_json,1,4096)),'_','') LIKE ? ESCAPE '\\'
              OR oc.namespace LIKE ? ESCAPE '\\' COLLATE NOCASE
              OR oc.scope_key LIKE ? ESCAPE '\\' COLLATE NOCASE
              OR oc.locator_key LIKE ? ESCAPE '\\' COLLATE NOCASE) ORDER BY o.id""",
            (project, _like(q), _like(_norm(q)), _like(q), _like(_norm(q)), _like(q), _like(_norm(q)), _like(q), _like(q), _like(q))).fetchall()
        for row in rows:
            fields = [row['identifier'], row['value_prefix'], row['provenance_prefix'], row['namespace'], row['scope_key'], row['locator_key']]
            names = sorted({m for s in fields for m in re.findall(r"[A-Za-z_$][\w$.:/-]*", s) if _matches_name(m, q)})
            if not names and q not in " ".join(fields).casefold():
                continue
            item = {"node_uuid": row['observation_id'], "node_type": "observation", "evidence_uuid": row['occurrence_id'],
                "match_names": names, "identifier": row['identifier'],
                "scope": {"namespace": row['namespace'], "scope_key": row['scope_key']},
                "source_locator": {"path": row['relative_path'], "locator": row['locator_key'],
                    "artifact_version_uuid": row['version_id'], "sha256": row['sha256']},
                "provenance_excerpt": row['provenance_prefix'],
                "linked_task_uuids": sorted(set((row['linked_tasks'] or '').split(','))) if row['linked_tasks'] else [],
                "linked_task_labels": [_task_label(db, task_id) for task_id in sorted(set((row['linked_tasks'] or '').split(','))) if task_id] if row['linked_tasks'] else [],
                "provenance_label": "indexed observation; task links are explicit finding scope references only"}
            (exact if row['identifier'].casefold() == q or any(n.casefold() == q for n in names) else similar).append(item)

        # Task title and journal hits are labeled mentions, never upgraded to findings.
        tasks = db.execute("""SELECT t.uid,t.id,a.alias,t.record_json FROM tasks t LEFT JOIN entity_aliases a
            ON a.entity_id=t.uid AND a.project_id=t.project_id AND a.entity_type='task'
            WHERE t.project_id=? AND t.archived_at IS NULL ORDER BY t.uid""", (project,)).fetchall()
        for task in tasks:
            record = json.loads(task['record_json'])
            title = record.get('title', '')
            if _matches_name(title, q) or _matches_name(task['alias'] or '', q):
                locators = sorted(set(re.findall(r"(?:docs|analysis|generated)/[\w./-]+", json.dumps(record))))
                item = {"node_uuid": task['uid'], "node_type": "task_mention", "evidence_uuid": task['uid'],
                    "match_names": [title] if _matches_name(title, q) else [task['alias']], "task_uuid": task['uid'],
                    "task_title": title, "linked_task_labels": [_task_label(db, task['uid'])],
                    "scope": {"status": record.get('status'), "summary": record.get('summary', '')},
                    "source_locator": locators, "provenance": "task metadata mention; not behavioral evidence"}
                similar.append(item)
            events = db.execute("SELECT event_id,kind,details_json FROM events WHERE task_id=? ORDER BY sequence", (task['id'],)).fetchall()
            for event in events:
                text = json.loads(event['details_json']).get('text', '')
                locators = sorted(set(re.findall(r"(?:docs|analysis|generated)/[\w./-]+", text)))
                variables = []
                for prefix in ("Audit index v1\n", "Reconstruction v1\n"):
                    if text.startswith(prefix):
                        try:
                            names = json.loads(text[len(prefix):]).get("variables", {})
                            variables = names if isinstance(names, list) else list(names) if isinstance(names, dict) else []
                        except (ValueError, TypeError, AttributeError):
                            variables = []
                        break
                matching_names = sorted({str(name) for name in variables if isinstance(name, str) and _matches_name(name, q)})
                if matching_names:
                    match_class = "exact" if any(name.casefold() == q for name in matching_names) else "similar"
                    item = {"node_uuid": task['uid'], "node_type": "task_variable_reference", "evidence_uuid": event['event_id'],
                        "task_uuid": task['uid'], "task_title": title, "linked_task_labels": [_task_label(db, task['uid'])],
                        "match_names": matching_names, "scope": {"event_kind": event['kind']},
                        "source_locator": locators, "provenance": "structured journal variable index; a lead to linked source, not behavioral proof"}
                    (exact if match_class == "exact" else similar).append(item)
                if not matching_names and _matches_name(text, q):
                    item = {"node_uuid": task['uid'], "node_type": "task_journal_mention", "evidence_uuid": event['event_id'],
                        "task_uuid": task['uid'], "task_title": title, "linked_task_labels": [_task_label(db, task['uid'])],
                        "match_names": [query.strip()], "scope": {"event_kind": event['kind']},
                        "source_locator": locators, "provenance": "journal mention; must be checked against cited source and comparison"}
                    similar.append(item)

    keyfn = lambda kind, item: (0 if kind == "exact" else 1, item['node_type'], item['node_uuid'], item['evidence_uuid'])
    combined = [("exact", item) for item in exact] + [("similar", item) for item in similar]
    combined.sort(key=lambda pair: keyfn(*pair))
    selected = [(kind, item) for kind, item in combined if after is None or tuple(keyfn(kind, item)) > after]
    page = selected[:limit]
    next_cursor = _cursor(q, list(keyfn(*page[-1]))) if len(selected) > len(page) and page else None
    for kind, item in page:
        item["match_class"] = kind
    return {"project_id": project, "query": query.strip(), "items": [item for _, item in page],
        "next_cursor": next_cursor, "coverage": {"exact_count": len(exact), "similar_count": len(similar),
            "candidate_rule": "exact identifier/name or case-insensitive substring in indexed node fields",
            "evidence_rule": "task mentions are labeled and never treated as evidence; inspect each cited artifact locator, scope, method and limitations"}}
