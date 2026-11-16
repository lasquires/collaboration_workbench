"""Disposable authenticated source-criticism fixture; credentials never reach Git."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
from tools.roadmap_store import RoadmapStore
from tools import roadmap_server as server, workspace_auth as auth


def main():
    with tempfile.TemporaryDirectory(prefix='source-browser-') as directory:
        root = Path(directory)
        server.STORE_PATH = root / 'workspace.sqlite3'
        store = RoadmapStore(server.STORE_PATH)
        project = store.read_v1('projects')['items'][0]['id']
        owner = store.read_v1('capabilities', project)['capabilities']['local_principal_id']
        with store._connection() as db:
            invite = auth.issue_invite(db, project, owner, 'member', ['contribute', 'review'])
            token = auth.redeem_invite(db, invite['secret'])['token']
            principal = auth.authenticate(db, token)['principal_id']
            owner_token = auth.issue_owner_token(db, owner)['token']
            db.commit()

        repositories = []
        for name in ('alpha', 'beta'):
            repo = root / name
            repo.mkdir()
            def git(*args):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            git('init', '-q')
            git('config', 'user.name', 'Synthetic Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            (repo / 'source.py').write_text(name + '_only = 1\n', encoding='utf-8')
            git('add', 'source.py')
            git('commit', '-qm', 'Synthetic source')
            repositories.append(str(repo))

        def write(op):
            return store.transact_v1({'project_id': project, 'actor': {'principal_id': owner},
                'expected_revision': store.snapshot()['revision'], 'request_id': str(uuid.uuid4()),
                'operations': [op]})

        task = write({'op': 'create', 'alias': 'source-acceptance', 'record': {
            'title': 'Synthetic source acceptance', 'status': 'active',
            'acceptance': ['Bounded browser fixture']}})['changed_records'][0]['id']
        write({'op': 'register_root', 'id': 'fixture-root', 'path': directory})
        versions = {}
        for name, build, content in [('build-a', 'build-a', 'speed = 2'),
                                     ('build-b', 'build-b', 'speed = 3'),
                                     ('report-a', None, 'report one from build a'),
                                     ('report-b', None, 'report two from build a')]:
            (root / (name + '.txt')).write_text(content, encoding='utf-8')
            write({'op': 'register_artifact', 'id': name, 'root_id': 'fixture-root',
                   'relative_path': name + '.txt', 'kind': 'source'})
            op = {'op': 'register_artifact_version', 'artifact_id': name}
            if build:
                op['binary_build_id'] = build
            versions[name] = write(op)['changed_records'][0]['id']

        for name, value in [('build-a', 2), ('build-b', 3)]:
            run = 'run-' + name
            version = versions[name]
            write({'op': 'create_index_run', 'id': run, 'extractor_id': 'synthetic',
                   'extractor_version': '1', 'config_hash': 'a' * 64,
                   'input_version_ids': [version], 'coverage': {'complete': False, 'excluded': ['Other lines']}})
            result = write({'op': 'stage_batch', 'run_id': run, 'batch_key': '0', 'records': [{
                'artifact_version_id': version, 'namespace': 'source', 'scope_key': 'Ball',
                'locator_key': {'line': 1}, 'identifier': 'speed', 'value': value,
                'provenance': {'method': 'Read synthetic fixture', 'limitations': 'No runtime measurement'}}]})
            digest = result['changed_records'][0]['payload_hash']
            write({'op': 'publish_run', 'run_id': run,
                   'manifest': {'batches': [{'key': '0', 'sha256': digest}], 'record_count': 1}})

        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        http.shared_mode = True
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        print(json.dumps({'base': f'http://127.0.0.1:{http.server_port}', 'project': project,
                          'principal': principal, 'token': token, 'task': task, 'versions': versions,
                          'owner_token': owner_token, 'repositories': repositories}), flush=True)
        try:
            # Keep the fixture alive until its parent sends a shutdown byte.
            # Production Git helpers must detach from this controller pipe.
            os.read(sys.stdin.fileno(), 1)
        finally:
            http.shutdown()
            thread.join()
            http.server_close()


if __name__ == '__main__':
    main()
