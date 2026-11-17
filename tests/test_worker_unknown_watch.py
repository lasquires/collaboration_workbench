"""Unknown provider watches must not invent an owner that excludes discoveries."""
import argparse
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch
import workspace
from tools import workbench_worker as worker


class UnknownWatchTests(unittest.TestCase):
    def test_watch_omits_unknown_owner_and_preserves_known_owner(self):
        for owner in (None,'KnownProvider'):
            sent=[]
            def request(url,path,payload=None,**kwargs):
                if '/context?' in path:return {'context':{'task':{'id':'task-id'}}}
                if path.endswith('/capabilities'):
                    return {'revision':'store:1','capabilities':{'local_principal_id':'principal'}}
                self.assertEqual(path,'/v1/transactions');sent.append(payload);return {'request_id':'saved'}
            args=argparse.Namespace(repo=Path('.'),url='http://127.0.0.1:8771',token_file=None,
                action='watch',task='task',variable='CGImage',owner=owner,namespace='selector',
                build=None,question='Which original provider supplies CGImage?')
            with self.subTest(owner=owner),patch.object(workspace,'load_manifest',return_value={'project_id':'project'}),patch.object(workspace,'request',side_effect=request):
                worker.run(args)
            operations=sent[0]['operations']
            criteria=operations[0]['criteria']['any_of'][0]
            self.assertEqual(criteria['identifier'],'CGImage')
            self.assertEqual(criteria['namespace'],'selector')
            self.assertEqual(criteria.get('scope_key'),owner)
            self.assertEqual('scope_key' in criteria,owner is not None)
            self.assertNotIn('binary_build_id',criteria)
            self.assertEqual(operations[1]['op'],'subscribe')
            self.assertEqual(operations[1]['target_id'],operations[0]['id'])

    def test_discovery_still_requires_owner(self):
        with patch('sys.argv',['worker','discover','task','--variable','CGImage']),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:worker.main()
        self.assertEqual(error.exception.code,2)


if __name__=='__main__':unittest.main()
