"""Offline regression tests: never contact Instagram or touch production data."""
import os, sys, tempfile, unittest
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
DATA = tempfile.TemporaryDirectory()
os.environ['REELS_DATA_DIR'] = DATA.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import config, auth, accounts, delivery, distributor, poster, reels, remover, statefile, web
from db import Session, Reel, PostingAccount, Delivery, AuthCommand, Config
from instagrapi.exceptions import ChallengeRequired, LoginRequired

class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        with Session() as s:
            for model in [Delivery, Reel, AuthCommand, PostingAccount, Config]:
                s.query(model).delete()
            s.commit()
        config.ACCOUNTS = ['source_a','source_b']
        config.IS_POST_TO_STORY = '0'
        config.REMOVE_FILE_AFTER_MINS = '1'
        self.alert = patch('notifier.alert_login_failed').start()
        self.addCleanup(patch.stopall)

    def account(self, name='a', **kwargs):
        accounts.add_account(name, password='test-only', **kwargs)
        return accounts.AccountRuntime(name, password='test-only')

    def reel(self, code='r1', owner='a', source='source_a'):
        path=Path(config.DOWNLOAD_DIR)/f'{code}.mp4'
        path.write_bytes(b'offline fixture')
        with Session() as s:
            s.add(Reel(code=code, post_id='123', account=source, assigned_to=owner,
                       file_path=str(path), is_posted=False))
            s.commit()
        return str(path)

    def test_saved_session_keeps_identity_without_password_login(self):
        p=Path(DATA.name)/'saved.json';p.write_text('{}')
        client=Mock()
        with patch.object(auth,'Client',return_value=client), patch.object(auth,'dump_session'):
            result,status,_=auth.login_account('a','pw',session_file=str(p),totp_secret='ABC')
        self.assertEqual(status,'ok');client.login.assert_not_called();client.set_uuids.assert_not_called()
        client.load_settings.assert_called_once_with(str(p), override_app_version=True)

    def test_challenge_does_not_fall_through_to_password_or_cookie(self):
        p=Path(DATA.name)/'challenge.json';p.write_text('{}')
        client=Mock();client.account_info.side_effect=ChallengeRequired()
        with patch.object(auth,'Client',return_value=client):
            _,status,_=auth.login_account('a','pw','cookie',str(p))
        self.assertEqual(status,'challenged');client.login.assert_not_called();self.assertTrue(p.exists())

    def test_transient_preserves_session(self):
        p=Path(DATA.name)/'transient.json';p.write_text('{}')
        client=Mock();client.account_info.side_effect=TimeoutError()
        with patch.object(auth,'Client',return_value=client):
            _,status,_=auth.login_account('a','pw',session_file=str(p))
        self.assertEqual(status,'transient');self.assertEqual(p.read_text(),'{}');client.login.assert_not_called()

    def test_totp_login_accepts_secret(self):
        client=Mock()
        with patch.object(auth,'Client',return_value=client), patch.object(auth,'dump_session'), patch.object(auth,'generate_totp_code',return_value='123456'):
            _,status,_=auth.login_account('a','pw',session_file=str(Path(DATA.name)/'missing'),is_2fa=True,totp_secret='key')
        self.assertEqual(status,'ok');client.login.assert_called_once_with('a','pw',verification_code='123456')

    def test_challenge_survives_restart_and_force(self):
        runtime=self.account()
        with patch.object(auth,'login_account',return_value=(None,'challenged','manual')) as login:
            runtime.ensure_login()
            until=accounts.get_account('a').next_login_at
            self.assertGreater(until,datetime.now()+timedelta(minutes=50))
            accounts.AccountRuntime('a').ensure_login(force=True)
            self.assertEqual(login.call_count,1)

    def test_transient_deadline_survives_restart(self):
        runtime=self.account()
        with patch.object(auth,'login_account',return_value=(None,'transient','timeout')) as login:
            runtime.ensure_login(); accounts.AccountRuntime('a').ensure_login(force=True)
            self.assertEqual(login.call_count,1)

    def test_dashboard_queues_2fa_without_network(self):
        self.account()
        with patch.object(auth,'login_with_2fa_code') as login:
            with web.app.test_request_context(json={'code':'123456'}):
                web.verify_2fa_api('a')
            login.assert_not_called()
        with Session() as s:self.assertEqual(s.query(AuthCommand).count(),1)

    def test_delivery_claim_unique_per_destination(self):
        self.assertTrue(delivery.claim('a','r'))
        self.assertFalse(delivery.claim('a','r'))
        self.assertTrue(delivery.claim('b','r'))

    def test_uncertain_upload_not_retried(self):
        self.account();self.reel()
        with patch.object(poster,'_upload',side_effect=TimeoutError()),patch.object(poster,'notify_discord'):
            self.assertFalse(poster.post_for_account(Mock(),'a'))
            self.assertIsNone(poster.get_reel('a'))
        with Session() as s:self.assertEqual(s.query(Delivery).one().status,'uncertain')

    def test_real_confirmation_required(self):
        self.reel();delivery.claim('a','r1')
        with self.assertRaises(ValueError):delivery.confirm('a','r1',SimpleNamespace(pk='unknown_pk'))

    def test_swap_allows_second_account(self):
        self.account();self.account('b');self.reel()
        delivery.claim('a','r1');delivery.confirm('a','r1',SimpleNamespace(pk=123,code='dest'))
        with patch('notifier.alert_swap'):
            self.assertEqual(distributor.swap_assignments(['a','b']),1)
        self.assertIsNotNone(poster.get_reel('b'));self.assertTrue(delivery.blocked('a','r1'))

    def test_restart_quarantines_inflight(self):
        self.reel();delivery.claim('a','r1');delivery.migrate_history()
        with Session() as s:self.assertEqual(s.query(Delivery).one().status,'uncertain')

    def test_notification_error_cannot_undo_confirmation(self):
        self.account();self.reel()
        with patch.object(poster,'_upload',return_value=(SimpleNamespace(pk=123,code='dest'),True)), patch.object(poster,'notify_discord',side_effect=OSError()):
            self.assertTrue(poster.post_for_account(Mock(),'a'))
        with Session() as s:self.assertEqual(s.query(Delivery).one().status,'confirmed')

    def test_cleanup_respects_age_and_keeps_history(self):
        path=self.reel();delivery.claim('a','r1');delivery.confirm('a','r1',SimpleNamespace(pk=123,code='dest'))
        remover.main();self.assertTrue(Path(path).exists())
        with Session() as s:s.query(Reel).update({'posted_at':datetime.now()-timedelta(minutes=2)});s.commit()
        remover.main();self.assertFalse(Path(path).exists());self.assertTrue(delivery.blocked('a','r1'))

    def test_rotation_does_not_deadlock_when_source_missing(self):
        self.account();accounts.update_account('a',last_source='source_a');self.reel()
        self.assertIsNotNone(poster.get_reel('a'))

    def test_older_cursor_is_used(self):
        import helpers
        helpers.save_config('SOURCE_CURSOR_source_a','older-page')
        client=Mock();client.user_clips_paginated_v1.return_value=([], 'next-page')
        reels.get_reels('source_a',client,older=True)
        self.assertEqual(client.user_clips_paginated_v1.call_args.kwargs['end_cursor'],'older-page')

    def test_history_never_reset(self):
        self.reel();delivery.claim('a','r1');delivery.confirm('a','r1',SimpleNamespace(pk=123,code='dest'))
        self.assertEqual(distributor.reset_all(['a']),0);self.assertTrue(delivery.blocked('a','r1'))

    def test_dashboard_requires_auth(self):
        response=web.app.test_client().get('/api/accounts')
        self.assertIn(response.status_code,(401,503))

    def test_account_timer_restored(self):
        self.account()
        deadline=datetime.now()+timedelta(minutes=10)
        accounts.update_account('a', next_post_at=deadline)
        pool=accounts.AccountPool()
        self.assertEqual(pool.refresh()[0].next_post_at,deadline)

    def test_watchdog_does_not_restart_challenged_queue(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('watchdog_under_test',Path(__file__).resolve().parents[1]/'watchdog.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.reel()
        with patch.object(module,'service_is_active',return_value=True), patch.object(module,'dashboard_healthy',return_value=True), patch.object(module,'restart_service') as restart:
            module.check_once();restart.assert_not_called()

    def test_expired_session_updates_app_not_device(self):
        p=Path(DATA.name)/'expired.json';p.write_text('{}')
        client=Mock();client.account_info.side_effect=[LoginRequired(),SimpleNamespace(pk=1)]
        client.get_settings.return_value={'uuids':{'uuid':'original-device'},'cookies':{'sessionid':'expired'},'authorization_data':{'sessionid':'expired'}}
        with patch.object(auth,'Client',return_value=client),patch.object(auth,'dump_session'):
            _,status,_=auth.login_account('a','pw',session_file=str(p))
        self.assertEqual(status,'ok')
        settings=client.set_settings.call_args.args[0]
        self.assertEqual(settings['uuids']['uuid'],'original-device')
        self.assertEqual(settings['cookies'],{})
        client.load_settings.assert_called_with(str(p),override_app_version=True)

    def test_thirty_reels_split_and_swap_once(self):
        self.account('a');self.account('b')
        for i in range(30):self.reel('batch'+str(i),owner=None,source='source_'+str(i%3))
        distributor.distribute_unassigned(['a','b'])
        self.assertEqual(distributor.pending_count('a'),15)
        self.assertEqual(distributor.pending_count('b'),15)
        with patch.object(poster,'_upload',side_effect=lambda *a: (SimpleNamespace(pk=123,code='destination'),True)), patch.object(poster,'notify_discord'),patch('notifier.alert_swap'):
            for account in ['a','b']:
                for _ in range(15):self.assertTrue(poster.post_for_account(Mock(),account))
            self.assertEqual(distributor.swap_assignments(['a','b']),30)
            for account in ['a','b']:
                for _ in range(15):self.assertTrue(poster.post_for_account(Mock(),account))
            self.assertEqual(distributor.swap_assignments(['a','b']),0)
        with Session() as session:self.assertEqual(session.query(Delivery).filter_by(status='confirmed').count(),60)

    def test_totp_known_vector(self):
        with patch('time.time',return_value=59):
            self.assertEqual(auth.generate_totp_code('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'),'287082')

def tearDownModule():
    import logging
    from db import engine
    engine.dispose()
    logging.shutdown()
    DATA.cleanup()

if __name__ == '__main__':unittest.main()
