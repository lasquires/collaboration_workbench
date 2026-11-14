"""Transactional foundation migration, using the store's existing connection.

Opening v1 upgrades it exactly once. The service owns its startup lock and backup
preflight; this module never opens a second writer or guesses historical dates.
"""
import json
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 12

FOUNDATION_DDL = [
    "CREATE TABLE projects(id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, goal TEXT NOT NULL DEFAULT '', acceptance_json TEXT NOT NULL DEFAULT '[]', entry_documents_json TEXT NOT NULL DEFAULT '[]', coverage_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1), created_by TEXT, created_at TEXT, updated_at TEXT, archived_at TEXT)",
    "CREATE TABLE principals(id TEXT PRIMARY KEY, label TEXT NOT NULL, identity_kind TEXT NOT NULL CHECK(identity_kind='trusted_local_label'), revision INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT, archived_at TEXT)",
    "CREATE TABLE local_principals(project_id TEXT NOT NULL REFERENCES projects(id),label TEXT NOT NULL,principal_id TEXT NOT NULL REFERENCES principals(id),PRIMARY KEY(project_id,label))",
    "CREATE TABLE agent_sessions(id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), created_at TEXT)",
    "CREATE TABLE project_members(project_id TEXT NOT NULL REFERENCES projects(id), principal_id TEXT NOT NULL REFERENCES principals(id), capabilities_json TEXT NOT NULL DEFAULT '[\"contribute\"]', PRIMARY KEY(project_id,principal_id))",
    "CREATE TABLE areas(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), parent_id TEXT, title TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_by TEXT REFERENCES principals(id), created_at TEXT, updated_at TEXT, archived_at TEXT, UNIQUE(project_id,id), FOREIGN KEY(project_id,parent_id) REFERENCES areas(project_id,id), CHECK(id<>parent_id))",
    "CREATE TABLE area_paths(project_id TEXT NOT NULL,area_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(project_id,area_id,path),FOREIGN KEY(project_id,area_id) REFERENCES areas(project_id,id))",
    "CREATE TABLE guidance_links(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),area_id TEXT,task_id TEXT,document_version_id TEXT,inheritance_scope TEXT NOT NULL CHECK(inheritance_scope IN ('self','descendants')),body TEXT NOT NULL,source_path TEXT,revision INTEGER NOT NULL DEFAULT 1,created_by TEXT REFERENCES principals(id),created_at TEXT,updated_at TEXT,archived_at TEXT,UNIQUE(project_id,id),FOREIGN KEY(project_id,area_id) REFERENCES areas(project_id,id),FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,uid))",
    "CREATE TABLE entity_aliases(project_id TEXT NOT NULL REFERENCES projects(id),entity_type TEXT NOT NULL,alias TEXT NOT NULL,entity_id TEXT NOT NULL,PRIMARY KEY(project_id,entity_type,alias))",
    "CREATE TABLE containment(project_id TEXT NOT NULL REFERENCES projects(id),child_id TEXT NOT NULL UNIQUE,parent_id TEXT,sibling_order INTEGER NOT NULL DEFAULT 0 CHECK(sibling_order>=0),required_for_parent INTEGER NOT NULL DEFAULT 1 CHECK(required_for_parent IN (0,1)),revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),PRIMARY KEY(project_id,child_id),FOREIGN KEY(project_id,child_id) REFERENCES tasks(project_id,uid),FOREIGN KEY(project_id,parent_id) REFERENCES tasks(project_id,uid),CHECK(child_id<>parent_id))",
    "CREATE TABLE workspace_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,project_id TEXT NOT NULL REFERENCES projects(id),request_id TEXT NOT NULL REFERENCES transactions(request_id) DEFERRABLE INITIALLY DEFERRED,actor_id TEXT REFERENCES principals(id),entity_type TEXT,entity_id TEXT,kind TEXT NOT NULL,before_json TEXT,after_json TEXT,rationale TEXT,recorded_at TEXT NOT NULL)",
    "CREATE INDEX workspace_events_project_sequence ON workspace_events(project_id,sequence)",
    "CREATE INDEX tasks_project_uid ON tasks(project_id,uid)",
    "CREATE INDEX containment_parent ON containment(project_id,parent_id,sibling_order,child_id)",
    "CREATE TRIGGER immutable_workspace_event_update BEFORE UPDATE ON workspace_events BEGIN SELECT RAISE(ABORT,'workspace events are append-only'); END",
    "CREATE TRIGGER immutable_workspace_event_delete BEFORE DELETE ON workspace_events BEGIN SELECT RAISE(ABORT,'workspace events are append-only'); END",
    "CREATE TRIGGER immutable_task_identity BEFORE UPDATE OF uid,project_id ON tasks WHEN OLD.uid IS NOT NULL AND (NEW.uid IS NOT OLD.uid OR NEW.project_id IS NOT OLD.project_id) BEGIN SELECT RAISE(ABORT,'task identity is immutable'); END",
    "CREATE TRIGGER immutable_alias_update BEFORE UPDATE ON entity_aliases BEGIN SELECT RAISE(ABORT,'aliases are not recycled'); END",
    "CREATE TRIGGER immutable_alias_delete BEFORE DELETE ON entity_aliases BEGIN SELECT RAISE(ABORT,'aliases are not recycled'); END",
    "CREATE TRIGGER task_alias_scope BEFORE INSERT ON entity_aliases WHEN NEW.entity_type='task' AND NOT EXISTS(SELECT 1 FROM tasks WHERE project_id=NEW.project_id AND uid=NEW.entity_id) BEGIN SELECT RAISE(ABORT,'task alias must remain in project'); END",
    "CREATE TRIGGER task_area_scope BEFORE UPDATE OF area_id ON tasks WHEN NEW.area_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM areas WHERE project_id=NEW.project_id AND id=NEW.area_id) BEGIN SELECT RAISE(ABORT,'task area must remain in project'); END",
    "CREATE TRIGGER containment_cycle_insert BEFORE INSERT ON containment WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM containment WHERE child_id=NEW.child_id) BEGIN SELECT CASE WHEN EXISTS(WITH RECURSIVE ancestors(id) AS (SELECT NEW.parent_id UNION SELECT c.parent_id FROM containment c JOIN ancestors a ON c.child_id=a.id WHERE c.project_id=NEW.project_id AND c.parent_id IS NOT NULL) SELECT 1 FROM ancestors WHERE id=NEW.child_id) THEN RAISE(ABORT,'containment cycle') END; END",
    "CREATE TRIGGER containment_cycle_update BEFORE UPDATE OF parent_id ON containment WHEN NEW.parent_id IS NOT NULL AND NEW.parent_id IS NOT OLD.parent_id BEGIN SELECT CASE WHEN EXISTS(WITH RECURSIVE ancestors(id) AS (SELECT NEW.parent_id UNION SELECT c.parent_id FROM containment c JOIN ancestors a ON c.child_id=a.id WHERE c.project_id=NEW.project_id AND c.parent_id IS NOT NULL) SELECT 1 FROM ancestors WHERE id=NEW.child_id) THEN RAISE(ABORT,'containment cycle') END; END",
    "CREATE TRIGGER area_cycle_update BEFORE UPDATE OF parent_id ON areas WHEN NEW.parent_id IS NOT NULL BEGIN SELECT CASE WHEN EXISTS(WITH RECURSIVE ancestors(id) AS (SELECT NEW.parent_id UNION SELECT a.parent_id FROM areas a JOIN ancestors p ON a.id=p.id WHERE a.project_id=NEW.project_id AND a.parent_id IS NOT NULL) SELECT 1 FROM ancestors WHERE id=NEW.id) THEN RAISE(ABORT,'area cycle') END; END",
]


def principal_for_label(db, project_id, label):
    # Labels are local attribution, never cross-installation authentication.
    found=db.execute('SELECT principal_id FROM local_principals WHERE project_id=? AND label=?',(project_id,label)).fetchone()
    if found:return found[0]
    installation=db.execute('SELECT store_id FROM store_meta WHERE singleton=1').fetchone()[0]
    namespace=uuid.uuid5(uuid.NAMESPACE_URL,'workspace-installation:'+installation)
    identity=str(uuid.uuid5(namespace,project_id+':trusted-local:'+label))
    db.execute("INSERT OR IGNORE INTO principals(id,label,identity_kind) VALUES(?,?,'trusted_local_label')", (identity,label))
    db.execute('INSERT OR IGNORE INTO project_members(project_id,principal_id) VALUES(?,?)', (project_id,identity))
    db.execute('INSERT INTO local_principals VALUES(?,?,?)',(project_id,label,identity))
    return identity


def default_project(db):
    return db.execute("SELECT id FROM projects ORDER BY CASE slug WHEN 'workspace' THEN 0 WHEN 'ssg2' THEN 1 ELSE 2 END,id LIMIT 1").fetchone()[0]


def _migration_checkpoint(db):
    """Fault-injection seam after DDL/backfill, before the migration commits."""


def sync_tasks(db, *, project_id=None, before=None, mirror_containment=True):
    """Mirror compatibility structure atomically; keep stable identities untouched."""
    project_id = project_id or default_project(db)
    rows = list(db.execute('SELECT * FROM tasks ORDER BY rowid'))
    for row in rows:
        if row['uid'] is None:
            uid = str(uuid.uuid4())
            db.execute('UPDATE tasks SET uid=?,project_id=? WHERE id=?', (uid,project_id,row['id']))
            db.execute("INSERT INTO entity_aliases VALUES(?,'task',?,?)", (project_id,row['id'],uid))
        if before is not None and row['id'] in before:
            old = before[row['id']]
            changed = (json.loads(row['record_json']) != old['record'] or row['parent_id'] != old['parent_id'] or row['archive_reason'] != old['archive_reason'])
            if changed:
                structure = row['parent_id'] != old['parent_id'] or row['archive_reason'] != old['archive_reason']
                db.execute('UPDATE tasks SET revision=revision+1,structure_revision=structure_revision+?,updated_at=? WHERE id=?', (int(structure),datetime.now(timezone.utc).isoformat(),row['id']))
    if not mirror_containment:
        for task in db.execute('SELECT project_id,record_json FROM tasks'):
            claim=json.loads(task['record_json']).get('claim')
            if claim: principal_for_label(db,task['project_id'],claim['by'])
        return
    # Clear changing parents before applying the final forest so valid bulk
    # moves cannot trip a trigger on a temporary intermediate cycle.
    mirrored=db.execute('SELECT t.*,p.uid AS parent_uid FROM tasks t LEFT JOIN tasks p ON p.id=t.parent_id ORDER BY t.rowid').fetchall()
    previous={r['child_id']:dict(r) for r in db.execute('SELECT * FROM containment')}
    for row in mirrored:
        old=previous.get(row['uid'])
        if old is not None and old['parent_id']!=row['parent_uid']:
            db.execute('UPDATE containment SET parent_id=NULL WHERE child_id=?',(row['uid'],))
    for row in mirrored:
        claim=json.loads(row['record_json']).get('claim')
        if claim:
            principal_for_label(db,row['project_id'],claim['by'])
        old=previous.get(row['uid'])
        changed=old is None or old['parent_id']!=row['parent_uid'] or old['sibling_order']!=row['sibling_order']
        if not changed:
            continue
        revision=1 if old is None else old['revision']+1
        db.execute('INSERT INTO containment(project_id,child_id,parent_id,sibling_order,revision) VALUES(?,?,?,?,?) ON CONFLICT(child_id) DO UPDATE SET parent_id=excluded.parent_id,sibling_order=excluded.sibling_order,revision=excluded.revision', (row['project_id'],row['uid'],row['parent_uid'],row['sibling_order'],revision))



def migrate(db, *, fresh=False, bootstrap_slug="ssg2"):
    version = db.execute('SELECT schema_version FROM store_meta WHERE singleton=1').fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
        raise ValueError('Unsupported task database schema')
    db.execute('BEGIN IMMEDIATE')
    try:
        meta = db.execute('SELECT * FROM store_meta').fetchone()
        if version>=2:
            db.execute('CREATE TABLE IF NOT EXISTS local_principals(project_id TEXT NOT NULL REFERENCES projects(id),label TEXT NOT NULL,principal_id TEXT NOT NULL REFERENCES principals(id),PRIMARY KEY(project_id,label))')
            # Preserve historical local UUIDs on upgrades without treating imported
            # remote labels as this installation's future default actor.
            for actor in db.execute('SELECT m.project_id,p.id,p.label FROM project_members m JOIN principals p ON p.id=m.principal_id').fetchall():
                legacy=str(uuid.uuid5(uuid.UUID(actor[0]),'trusted-local:'+actor[2]))
                if actor[1]==legacy:
                    db.execute('INSERT OR IGNORE INTO local_principals VALUES(?,?,?)',(actor[0],actor[2],actor[1]))
        if version == 1:
            for column in [
                'uid TEXT', 'project_id TEXT REFERENCES projects(id)',
                'revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1)',
                'structure_revision INTEGER NOT NULL DEFAULT 1 CHECK(structure_revision>=1)',
                'area_id TEXT', 'decomposition_complete INTEGER NOT NULL DEFAULT 0 CHECK(decomposition_complete IN (0,1))',
                "acceptance_state TEXT NOT NULL DEFAULT 'unknown' CHECK(acceptance_state IN ('unknown','pending','passed','invalidated'))",
                'validation_id TEXT', 'created_by TEXT REFERENCES principals(id)',
                'created_at TEXT', 'updated_at TEXT', 'archived_at TEXT',
            ]:
                db.execute('ALTER TABLE tasks ADD COLUMN ' + column)
            db.execute('CREATE UNIQUE INDEX tasks_uid_unique ON tasks(uid)')
            db.execute('CREATE UNIQUE INDEX tasks_project_identity ON tasks(project_id,uid)')
            for statement in FOUNDATION_DDL:
                db.execute(statement)
            project = str(uuid.uuid5(uuid.NAMESPACE_URL, 'ssg2-workspace:' + meta['store_id']))
            slug=bootstrap_slug if fresh else 'ssg2'
            if not isinstance(slug,str) or not slug or len(slug)>80: raise ValueError('Invalid bootstrap project slug')
            goal='Restore original SSG2 single-player experience' if slug=='ssg2' else 'Research workspace'
            db.execute("INSERT INTO projects(id,slug,goal,coverage_json) VALUES(?,?,?,?)", (project,slug,goal,json.dumps({'source':'legacy task import' if slug=='ssg2' else 'empty workspace','complete':False,'uncovered':['research collections']})))
            principal_for_label(db,project,'local')
            sync_tasks(db,project_id=project)
        try:
            from . import roadmap_research, roadmap_matcher, roadmap_documents, roadmap_structure, roadmap_exchange
        except ImportError:
            import roadmap_research, roadmap_matcher, roadmap_documents, roadmap_structure, roadmap_exchange
        if version <= 2:
            roadmap_research.create_schema(db)
            roadmap_matcher.create_schema(db)
            roadmap_documents.create_schema(db)
        if version <= 3: roadmap_structure.create_schema(db)
        try:
            from . import workspace_merge_compensation
        except ImportError:
            import workspace_merge_compensation
        workspace_merge_compensation.create_schema(db)
        try:
            from . import workspace_task_namespace
        except ImportError:
            import workspace_task_namespace
        workspace_task_namespace.create_schema(db)
        try:
            from . import workspace_findings
        except ImportError:
            import workspace_findings
        workspace_findings.create_schema(db)
        roadmap_exchange.create_schema(db)
        try:
            from . import roadmap_projects
        except ImportError:
            import roadmap_projects
        roadmap_projects.create_schema(db)
        try:
            from . import workspace_repository_service,workspace_auth
        except ImportError:
            import workspace_repository_service,workspace_auth
        workspace_repository_service.create_schema(db)
        workspace_auth.create_schema(db)
        _migration_checkpoint(db)
        db.execute('UPDATE store_meta SET schema_version=?,revision=revision+?', (SCHEMA_VERSION,0 if fresh else 1))
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Foundation migration foreign key failure')
        db.commit()
    except Exception:
        db.rollback()
        raise
