import tempfile,time,unittest,json
from pathlib import Path
from unittest.mock import patch
import test_reliability
import media_host

class MediaTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  base=Path(self.temp.name);self.base=base
  self.root=base/'.media';self.root.mkdir();(self.root/'origin.json').write_text(json.dumps({'url':'https://test.trycloudflare.com'}))
  for target,value in [('ROOT',self.root)]:
   p=patch.object(media_host,target,value);p.start();self.addCleanup(p.stop)
  p=patch.object(media_host.config,'BASE_DIR',str(base));p.start();self.addCleanup(p.stop)
  self.client=media_host.app.test_client()
 def test_range_head_and_expiry(self):
  p=self.base/'video.mp4';p.write_bytes(b'0123456789')
  url,ticket=media_host.stage(p);path='/m/'+url.split('/m/')[1]
  response=self.client.get(path,headers={'Range':'bytes=2-4'})
  self.assertEqual(response.status_code,206);self.assertEqual(response.data,b'234');response.close()
  response=self.client.head(path);self.assertEqual(response.status_code,200);response.close()
  (self.root/ticket/'expires').write_text(str(time.time()-1))
  self.assertEqual(self.client.get(path).status_code,404)
  media_host.cleanup();self.assertFalse((self.root/ticket).exists());self.assertTrue(p.exists())
 def test_private_paths_and_management_not_exposed(self):
  p=self.base/'.private.mp4';p.write_bytes(b'secret')
  with self.assertRaises(ValueError):media_host.stage(p)
  for path in ['/','/official','/api/accounts','/.official/a/token.json','/m/../database/sqlite.db']:
   self.assertEqual(self.client.get(path).status_code,404)
