import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import test_db as fixtures
from test_main import callback_update

from app.db import Database
from app.handlers import Deps, build_router
from app.handlers.community import CommunityHandler
from app.handlers.community import Draft as EventDraft
from app.handlers.homework import Draft, HomeworkHandler
from app.services import form_drafts
from app.services.schedule_parser import Lesson
from app.timeutil import SAMARA


@unittest.skipUnless(fixtures.TEST_URL, "Requires isolated PostgreSQL")
class FormDraftTests(unittest.TestCase):
    # PostgreSQLTests supplies disposable schema and migration assertions.
    def setUp(self):
        fixtures.PostgreSQLTests.setUp(self)
        self.store.start(11)
        self.store.choose_group(11,self.groups['А-01'])

    def persist(self, actor, draft, handler=None):
        handler=handler or HomeworkHandler()
        handler.drafts.put(actor,draft)
        router=build_router(Deps.create(Mock(),self.store,90),homework=handler if isinstance(handler,HomeworkHandler) else None,
                            community=handler if isinstance(handler,CommunityHandler) else None)
        with self.store.connect() as conn:form_drafts.save(conn,actor,router.handlers)
        return router

    def test_restart_recovers_private_attachment_and_cancel_is_durable(self):
        draft=Draft(self.groups['А-01'],False,subject='История',files=[('private.txt',b'private')])
        self.persist(11,draft)
        router=build_router(Deps.create(Mock(),self.store,90))
        router.dispatch(callback_update('hw:resume',user_id=11))
        handler=next(h for h in router.handlers if isinstance(h,HomeworkHandler))
        self.assertEqual(handler.drafts[11].files,[['private.txt',b'private']])
        router.dispatch(callback_update('hw:resume',user_id=12))
        self.assertNotIn(12,handler.drafts)
        router.dispatch(callback_update('hw:cancel',user_id=11))
        with self.store.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM form_drafts').fetchone(),(0,))
        router=build_router(Deps.create(Mock(),self.store,90))
        router.dispatch(callback_update('hw:resume',user_id=11))
        self.assertFalse(any(h.drafts and 11 in h.drafts for h in router.handlers))

    def test_expiry_and_only_json_not_pickle(self):
        router=self.persist(11,Draft(self.groups['А-01'],False))
        with self.store.connect() as conn:
            conn.execute("UPDATE form_drafts SET expires_at=now()-interval '1 second'")
        router.dispatch(callback_update('hw:resume',user_id=11))
        self.assertFalse(any(h.drafts and 11 in h.drafts for h in router.handlers))
        with self.assertRaises(ValueError):form_drafts.decode({'$pickle':'bad'})
        with self.assertRaises(ValueError):form_drafts.encode(object())

    def test_atomic_publication_rollback_then_retry_and_stale_confirmation(self):
        day=datetime.now(SAMARA).date()
        self.store.seed_legacy('synthetic-draft', ['А-01'], [Lesson('А-01',0,1,'all','История','','','',())],day,day+timedelta(days=7),None)
        draft=Draft(self.groups['А-01'],False,step='preview',subject='История',body='Задание')
        self.persist(11,draft)
        router=build_router(Deps.create(Mock(),self.store,90))
        action='hw:d:'+draft.token+':yes'
        original=form_drafts.save
        with patch.object(form_drafts,'save',side_effect=RuntimeError('simulated interruption')):
            router.dispatch(callback_update(action,user_id=11))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM homework').fetchone(),(0,))
            self.assertEqual(conn.execute('SELECT count(*) FROM form_drafts').fetchone(),(1,))
        with patch.object(form_drafts,'save',original):router.dispatch(callback_update(action,user_id=11))
        router=build_router(Deps.create(Mock(),self.store,90))
        router.dispatch(callback_update(action,user_id=11))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM homework').fetchone(),(1,))
            self.assertEqual(conn.execute('SELECT count(*) FROM form_drafts').fetchone(),(0,))

    def test_event_media_survives_and_permissions_rechecked(self):
        draft=EventDraft('event',title='Private draft',starts_at=datetime.now(SAMARA)+timedelta(days=3),step=6,media=[])
        self.persist(90,draft,CommunityHandler())
        router=build_router(Deps.create(Mock(),self.store,None))
        router.dispatch(callback_update('community:d:'+draft.token+':save',user_id=90))
        with self.store.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone(),(0,))

    def test_borrowed_savepoint_error_does_not_poison_outer_and_connection_returns(self):
        db=Database(self.store.dsn,pool_size=1)
        self.addCleanup(db.close)
        with db.unit_of_work() as conn:
            pid=conn.info.backend_pid
            try:
                with db.connect() as borrowed:borrowed.execute('SELECT 1/0')
            except Exception:pass
            self.assertEqual(conn.execute('SELECT 1').fetchone(),(1,))
            with db.connect() as borrowed:self.assertEqual(borrowed.info.backend_pid,pid)
        with db.connect() as conn:self.assertEqual(conn.info.backend_pid,pid)

    def test_polling_lease_denies_duplicate_and_releases_without_pool_leaks(self):
        from app.db.polling_lease import PollingLease
        first=PollingLease(self.store.dsn,'synthetic-only')
        second=PollingLease(self.store.dsn,'synthetic-only')
        first.open()
        try:
            with self.assertRaises(ValueError):second.open()
            first.check()
        finally:first.close()
        second.open()
        second.check()
        second.close()
        with self.assertRaises(AttributeError):second.check()
