import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_reliability
import official

class OfficialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = patch.object(official, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.root.stop)
        official.save(official.folder('a')/'token.json', {'id':'123', 'token':'private', 'saved':time.time()})

    def test_wrong_account_rejected(self):
        with patch.object(official, 'request', return_value={'user_id':'123','username':'b'}):
            with self.assertRaises(ValueError): official.connect('a','wrong')
        self.assertEqual(official.read(official.folder('a')/'token.json')['token'], 'private')

    def test_status_does_not_expose_token(self):
        self.assertNotIn('private', str(official.status('a')))

    def test_ambiguous_publish_never_retried(self):
        official.enqueue('a','https://example.com/a.mp4','','caption')
        job_path = next(official.folder('a').glob('job-*'))
        job = official.read(job_path)
        job.update(state='processing', container='456')
        official.save(job_path, job)
        with patch.object(official, 'request', side_effect=[{'status_code':'FINISHED'}, {'data':[{'quota_usage':0,'config':{'quota_total':50}}]}, official.APIError('Timeout')]) as api:
            self.assertFalse(official.tick('a'))
            self.assertEqual(api.call_count,3)
        with patch.object(official, 'request') as api:
            official.tick('a')
            api.assert_not_called()
        self.assertEqual(official.read(job_path)['state'],'uncertain')

    def test_published_id_is_required_and_saved(self):
        official.enqueue('a','https://example.com/b.mp4','','caption')
        path = next(official.folder('a').glob('job-*'))
        job=official.read(path); job.update(state='processing',container='456');official.save(path,job)
        with patch.object(official,'request',side_effect=[{'status_code':'FINISHED'},{'data':[{'quota_usage':0,'config':{'quota_total':50}}]},{'id':'789'}]):
            self.assertTrue(official.tick('a'))
        self.assertEqual(official.read(path)['media_id'],'789')

    def test_local_urls_rejected(self):
        for url in ['http://example.com/a.mp4','https://192.168.29.60/a.mp4','https://electro.local/a.mp4']:
            with self.assertRaises(ValueError): official.public_url(url)

    def test_quota_blocks_publish(self):
        official.enqueue('a','https://example.com/c.mp4','','caption')
        path=next(official.folder('a').glob('job-*'));job=official.read(path);job.update(state='processing',container='456');official.save(path,job)
        with patch.object(official,'request',side_effect=[{'status_code':'FINISHED'},{'data':[{'quota_usage':50,'config':{'quota_total':50}}]}]) as api:
            self.assertFalse(official.tick('a'))
            self.assertEqual(api.call_count,2)
