import json
import unittest
import uuid
from tools import roadmap_exchange as exchange
from tools.workspace_sync import WorkspaceGitSync,SyncConflict
from tools.workspace_live_client import LiveConflict
from tests import test_workspace_sync as git_fixture
from tests import test_workspace_live_client as live_fixture


def transact(store,project,actor,operations):
    return store.transact_v1({'project_id':project,'actor':{'principal_id':actor},'request_id':str(uuid.uuid4()),'expected_revision':store.snapshot()['revision'],'operations':operations})


def seed(store,project,actor):
    transact(store,project,actor,[{'op':'create','alias':name,'record':{'title':name,'status':'ready'}} for name in ('LINK-A','LINK-B')]+[{'op':'add_link','task_id':'LINK-A','target_id':'LINK-B','kind':'related','rationale':'Original relationship'}])


def remove(store,project,actor):
    return transact(store,project,actor,[{'op':'remove_link','task_id':'LINK-A','target_id':'LINK-B','kind':'related'}])


def links(store):
    with store._connection() as db:return [dict(row) for row in db.execute('SELECT * FROM task_links')]


class GitLinkSyncTests(unittest.TestCase):
    setUp=git_fixture.WorkspaceSyncTests.setUp

    def shared_links(self):
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        seed(self.stores[0],self.project,self.actor)
        self.syncs[0].sync_now();self.syncs[1].sync_now();self.syncs[0].sync_now()
        self.assertEqual(len(links(self.stores[1])),1)

    def test_removed_relationship_stays_removed_through_git_and_restart(self):
        self.shared_links();remove(self.stores[0],self.project,self.actor)
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        for store in self.stores:self.assertEqual(links(store),[])
        restarted=WorkspaceGitSync(self.stores[1],self.project,self.actor,str(self.remote),self.root/'cache-1',export_selected=True)
        self.assertFalse(restarted.sync_now().pushed)
        self.assertEqual(restarted.transport.fetch().payload['records']['task_links'],[])

    def test_remote_deletion_conflicts_with_concurrent_link_edit(self):
        self.shared_links();remove(self.stores[0],self.project,self.actor)
        with self.stores[1]._connection() as db:db.execute("UPDATE task_links SET rationale='Concurrent evidence' WHERE kind='related'")
        self.syncs[0].sync_now()
        with self.assertRaises(SyncConflict):self.syncs[1].sync_now()
        review=self.syncs[1].preview_remote()
        change=next(item for item in review['preview']['changes'] if item['table']=='task_links' and item['status']=='conflict')
        alternatives=change['fields']['$presence']
        self.assertEqual(alternatives['local']['rationale'],'Concurrent evidence')
        self.assertIsNone(alternatives['remote'])
        choices={exchange.canonical([change['table'],change['key'],'$presence']):'remote'}
        self.syncs[1].apply_remote(choices,review['tip'],review['bundle_id'],review['local_revision'])
        self.assertEqual(links(self.stores[1]),[])

    def test_stale_client_merges_multiple_missed_commits_without_deleting_unseen_links(self):
        self.shared_links()
        transact(self.stores[0],self.project,self.actor,[{'op':'add_link','task_id':'LINK-B','target_id':'LINK-A','kind':'related','rationale':'New remote relationship'}])
        self.syncs[0].sync_now()
        transact(self.stores[0],self.project,self.actor,[{'op':'create_question','id':'later-question','text':'A later remote update'}])
        self.syncs[0].sync_now()
        self.syncs[1].sync_now()
        self.assertEqual(len(links(self.stores[1])),2)
        self.assertEqual(self.stores[1].read_v1('questions',self.project)['items'][0]['id'],'later-question')

    def test_dependency_removal_updates_task_fields_and_preserves_other_links(self):
        self.shared_links()
        transact(self.stores[0],self.project,self.actor,[{'op':'update','task_id':'LINK-A','fields':{'dependencies':['LINK-B']}}])
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        transact(self.stores[0],self.project,self.actor,[{'op':'remove_link','task_id':'LINK-A','target_id':'LINK-B','kind':'depends_on'}])
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        for store in self.stores:
            context=store.read_v1('context',self.project,key='LINK-A')['context']
            self.assertEqual(context['fields']['dependencies'],[])
            self.assertEqual([row['kind'] for row in links(store)],['related'])

    def test_local_removal_versus_remote_edit_can_keep_the_reviewed_removal(self):
        self.shared_links();remove(self.stores[0],self.project,self.actor)
        with self.stores[1]._connection() as db:db.execute("UPDATE task_links SET rationale='Remote revised support' WHERE kind='related'")
        self.syncs[1].sync_now()
        with self.assertRaises(SyncConflict):self.syncs[0].sync_now()
        review=self.syncs[0].preview_remote()
        change=next(item for item in review['preview']['changes'] if item['table']=='task_links' and item['status']=='conflict')
        self.assertIsNone(change['fields']['$presence']['local'])
        self.assertEqual(change['fields']['$presence']['remote']['rationale'],'Remote revised support')
        choice=exchange.canonical(['task_links',change['key'],'$presence'])
        self.syncs[0].apply_remote({choice:'local'},review['tip'],review['bundle_id'],review['local_revision'])
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        for store in self.stores:self.assertEqual(links(store),[])
        with self.stores[0]._connection() as db:
            audit=json.loads(db.execute('SELECT result_json FROM exchange_imports WHERE id=?',(review['bundle_id'],)).fetchone()[0])
        saved=next(item for item in audit['preview']['changes'] if item['table']=='task_links' and item['status']=='conflict')
        self.assertEqual(saved['fields']['$presence'],change['fields']['$presence'])

    def test_rejected_push_reconciles_remote_removal_and_preserves_local_progress(self):
        self.shared_links()
        transact(self.stores[0],self.project,self.actor,[{'op':'create_question','id':'local-progress','text':'Keep this independent progress'}])
        original=self.syncs[0].transport._run;injected=[False]
        def raced(*args,**kwargs):
            if args and args[0]=='push' and not injected[0]:
                injected[0]=True
                remove(self.stores[1],self.project,self.actor);self.syncs[1].sync_now()
            return original(*args,**kwargs)
        self.syncs[0].transport._run=raced
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        self.assertTrue(injected[0])
        for store in self.stores:
            self.assertEqual(links(store),[])
            self.assertEqual(store.read_v1('questions',self.project)['items'][0]['id'],'local-progress')

    def test_new_installation_bootstraps_latest_snapshot_without_inventing_a_common_base(self):
        from tools.roadmap_store import RoadmapStore
        self.shared_links()
        fresh=RoadmapStore(self.root/'new-installation.sqlite3',bootstrap_slug='workspace')
        admin=fresh.read_v1('projects')['items'][0]['id'];actor=fresh.read_v1('capabilities',admin)['capabilities']['local_principal_id']
        transact(fresh,admin,actor,[{'op':'create_project','id':self.project,'slug':'shared'}])
        actor=fresh.read_v1('capabilities',self.project)['capabilities']['local_principal_id']
        sync=WorkspaceGitSync(fresh,self.project,actor,str(self.remote),self.root/'new-cache',export_selected=True)
        review=sync.preview_remote()
        self.assertIsNone(review['preview']['merge_base_id'])
        choices={exchange.canonical([change['table'],change['key'],field]):'remote' for change in review['preview']['changes'] for field,alternative in change['fields'].items() if alternative['conflict']}
        sync.apply_remote(choices,review['tip'],review['bundle_id'],review['local_revision'])
        self.assertEqual(len(links(fresh)),1)
        self.assertEqual(sync.transport.accepted().payload['id'],review['bundle_id'])

    def test_keeping_concurrently_edited_dependency_restores_consistent_task_fields(self):
        self.shared_links()
        transact(self.stores[0],self.project,self.actor,[{'op':'update','task_id':'LINK-A','fields':{'dependencies':['LINK-B']}}])
        self.syncs[0].sync_now();self.syncs[1].sync_now()
        transact(self.stores[0],self.project,self.actor,[{'op':'remove_link','task_id':'LINK-A','target_id':'LINK-B','kind':'depends_on'}])
        with self.stores[1]._connection() as db:db.execute("UPDATE task_links SET rationale='New dependency evidence' WHERE kind='depends_on'")
        self.syncs[1].sync_now()
        with self.assertRaises(SyncConflict):self.syncs[0].sync_now()
        review=self.syncs[0].preview_remote()
        change=next(item for item in review['preview']['changes'] if item['table']=='task_links' and item['status']=='conflict')
        choices={exchange.canonical(['task_links',change['key'],'$presence']):'remote'}
        self.syncs[0].apply_remote(choices,review['tip'],review['bundle_id'],review['local_revision'])
        context=self.stores[0].read_v1('context',self.project,key='LINK-A')['context']
        self.assertEqual(context['fields']['dependencies'],['LINK-B'])
        self.assertEqual([row['alias'] for row in context['dependencies']],['LINK-B'])

    def test_unknown_base_omission_cannot_remove_local_relationship(self):
        self.shared_links()
        with self.stores[0]._connection() as db:incoming=exchange.export_bundle(db,self.project,portable=True)
        incoming['records']['task_links']=[];incoming['ancestor_id']=None
        incoming['id']=exchange.research.digest({key:value for key,value in incoming.items() if key!='id'})
        self.syncs[1]._import(incoming)
        self.assertEqual(len(links(self.stores[1])),1)


class LiveLinkSyncTests(unittest.TestCase):
    setUp=live_fixture.LiveClientTests.setUp

    def test_live_client_removal_reaches_host_and_other_client(self):
        seed(self.stores[1],self.project,self.actors[1])
        self.clients[0].sync_once();self.clients[1].sync_once();self.clients[0].sync_once()
        remove(self.stores[1],self.project,self.actors[1])
        self.clients[0].sync_once();self.clients[1].sync_once()
        for store in self.stores:self.assertEqual(links(store),[])
        self.assertEqual(self.clients[0].sync_once()['state'],'current')
