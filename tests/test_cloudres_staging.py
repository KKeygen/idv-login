import base64
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = str(Path(__file__).resolve().parents[1] / 'tools')
sys.path.insert(0, TOOLS)
try:
    import markdown  # noqa: F401
except ModuleNotFoundError:
    sys.modules['markdown'] = types.SimpleNamespace(markdown=lambda value, **_: value)
from cloudres_staging import STAGING_BRANCH, staging_snapshot
import update_cloud_resources
import unpack

MAIN = 'a' * 40
STAGING = 'b' * 40
NEW_STAGING = 'c' * 40

class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status
    def json(self):
        return self.data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP ' + str(self.status_code))

class FakeSession:
    def __init__(self, *, diverged=False, merge_conflict=False, put_conflict=False):
        self.headers = {}
        self.diverged = diverged
        self.merge_conflict = merge_conflict
        self.put_conflict = put_conflict
        self.merges = []
        self.puts = []
        self.reads = []
        self.state = {'hotfix': [{'target_module': 'pending'}], 'data': []}
    def get(self, url, **kwargs):
        self.reads.append((url, kwargs))
        if url.endswith('/commits/main'):
            return Response({'sha': MAIN})
        if url.endswith('/commits/' + STAGING_BRANCH):
            return Response({'sha': NEW_STAGING if self.merges else STAGING})
        if '/compare/' in url:
            return Response({'merge_base_commit': {'sha': ('d' * 40 if self.diverged and not self.merges else MAIN)}})
        if '/contents/' in url:
            path = url.split('/contents/', 1)[1]
            payload = {'assets/cloudRes.json': json.dumps(self.state),
                       'assets/index.html': '<html>main</html>', 'assets/anno': 'main anno'}[path]
            return Response({'sha': 'blob-' + str(len(self.puts)),
                             'content': base64.b64encode(payload.encode()).decode()})
        raise AssertionError(url)
    def post(self, url, **kwargs):
        self.merges.append(kwargs['json'])
        return Response({}, 409 if self.merge_conflict else 201)
    def put(self, url, **kwargs):
        payload = kwargs['json']
        self.puts.append(payload)
        if self.put_conflict and len(self.puts) == 1:
            self.state['data'].append({'game_id': 'other-pending'})
            return Response({}, 409)
        return Response({}, 200)

class StagingTests(unittest.TestCase):
    def test_existing_main_ancestor_has_no_merge(self):
        s = FakeSession()
        self.assertEqual(staging_snapshot(s, 'root'), (MAIN, STAGING))
        self.assertEqual(s.merges, [])
    def test_behind_branch_merges_fixed_main_snapshot_and_keeps_history(self):
        s = FakeSession(diverged=True)
        self.assertEqual(staging_snapshot(s, 'root'), (MAIN, NEW_STAGING))
        self.assertEqual(s.merges[0]['base'], 'cloud-config-staging')
        self.assertEqual(s.merges[0]['head'], MAIN)
    def test_merge_conflict_fails_before_candidate_write(self):
        s = FakeSession(diverged=True, merge_conflict=True)
        with self.assertRaisesRegex(RuntimeError, 'resolve conflicts'):
            staging_snapshot(s, 'root')
        self.assertEqual(s.puts, [])
    def test_candidate_uses_staging_and_retries_without_losing_pending_data(self):
        s = FakeSession(put_conflict=True)
        env = {'GITHUB_REPOSITORY':'owner/repo', 'GITHUB_TOKEN':'fake', 'GITEE_ROPE':'owner/repo'}
        with patch.dict(os.environ, env, clear=True), patch.object(update_cloud_resources.requests, 'Session', return_value=s), patch.object(update_cloud_resources.time, 'sleep'):
            update_cloud_resources.main()
        self.assertEqual(len(s.puts), 2)
        self.assertTrue(all(p['branch'] == 'cloud-config-staging' for p in s.puts))
        payload = json.loads(base64.b64decode(s.puts[-1]['content']))
        self.assertEqual(payload['hotfix'], [{'target_module': 'pending'}])
        self.assertEqual(payload['data'], [{'game_id': 'other-pending'}])
        reads = [(u.split('/contents/',1)[1], k['params']['ref']) for u,k in s.reads if '/contents/' in u]
        self.assertIn(('assets/cloudRes.json', STAGING), reads)
        self.assertIn(('assets/index.html', MAIN), reads)
        self.assertNotEqual(s.puts[0]['sha'], s.puts[1]['sha'])
    def test_apk_update_targets_staging_and_preserves_concurrent_candidate(self):
        s = FakeSession(put_conflict=True)
        item = {'app_channel': 'test_channel', 'game_id': 'test_game'}
        with patch.object(unpack.requests, 'Session', return_value=s), patch.object(unpack.time, 'sleep'):
            unpack.update_cloud_res(item, token='fake')
        self.assertEqual(len(s.puts), 2)
        self.assertTrue(all(p['branch'] == 'cloud-config-staging' for p in s.puts))
        payload = json.loads(base64.b64decode(s.puts[-1]['content']))
        self.assertIn({'game_id': 'other-pending'}, payload['data'])
        self.assertIn(item, payload['data'])
        self.assertEqual(payload['hotfix'], [{'target_module': 'pending'}])

if __name__ == '__main__':
    unittest.main()
