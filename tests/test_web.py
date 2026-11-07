"""Synthetic signatures and WSGI checks; no real MAX credentials or network."""
import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlencode

import psycopg
from app.web_auth import verify_init_data,InvalidLaunch
from app.web import MiniApp,ReadViews

TOKEN='synthetic-test-token'


def signed(actor=10,stamp=None,**extra):
    fields={'auth_date':str(int(time.time()) if stamp is None else stamp),'user':json.dumps({'id':actor,'first_name':'Имя + & ='},ensure_ascii=False),'query_id':'test-launch'}|extra
    canonical='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    key=hmac.new(b'WebAppData',TOKEN.encode(),hashlib.sha256).digest()
    return urlencode(fields|{'hash':hmac.new(key,canonical.encode(),hashlib.sha256).hexdigest()})


def call(app,path='/api/schedule',raw=None,**extra):
    response=[]
    environ={'REQUEST_METHOD':'GET','PATH_INFO':path,'QUERY_STRING':'','HTTP_X_MAX_INIT_DATA':signed() if raw is None else raw}|extra
    body=b''.join(app(environ,lambda status,headers:response.append((status,dict(headers)))))
    return response[0][0],response[0][1],body


class WebSecurityTests(unittest.TestCase):
    def test_valid_unicode_launch_and_time_boundaries(self):
        self.assertEqual(verify_init_data(signed(stamp=1000),TOKEN,now=1300),10)
        for stamp in (999,1331):
            with self.assertRaises(InvalidLaunch):
                verify_init_data(signed(stamp=stamp),TOKEN,now=1300)

    def test_tampering_token_duplicate_fields_and_malformed_data_fail_closed(self):
        valid=signed()
        for raw in ('',valid+'&user=%7B%22id%22%3A11%7D',valid+'&hash='+'0'*64,
                    valid.replace('test-launch','forged'),valid+'&x=%ZZ',valid+'&x=%FF','x'*9000):
            with self.subTest(raw=raw[:25]),self.assertRaises(InvalidLaunch):
                verify_init_data(raw,TOKEN)
        with self.assertRaises(InvalidLaunch):
            verify_init_data(valid,'another-token')
        with self.assertRaises(InvalidLaunch):
            verify_init_data(valid,'')
        for actor in (True,0,-1,'10',2**63):
            with self.assertRaises(InvalidLaunch):
                verify_init_data(signed(actor=actor),TOKEN)

    def test_api_never_uses_unauthenticated_or_query_identity(self):
        views=Mock()
        app=MiniApp(views,TOKEN)
        for path in ('/api/schedule','/api/homework','/api/events'):
            self.assertEqual(call(app,path,raw='')[0],'401 Unauthorized')
        self.assertEqual(call(app,QUERY_STRING='user_id=11')[0],'400 Bad Request')
        views.read.assert_not_called()
        views.read.return_value={'items':[]}
        self.assertEqual(call(app)[0],'200 OK')
        views.read.assert_called_once_with(10,'/api/schedule')

    def test_demo_switch_checked_before_read_and_before_response(self):
        views,policy=Mock(),Mock()
        app=MiniApp(views,TOKEN,demo_policy=policy)
        policy.permits.return_value=False
        self.assertEqual(call(app)[0],'403 Forbidden')
        views.read.assert_not_called()
        policy.permits.side_effect=[True,False]
        views.read.return_value={'items':['must not leak']}
        status,headers,body=call(app)
        self.assertEqual(status,'403 Forbidden')
        self.assertNotIn(b'must not leak',body)

    def test_no_write_api_cors_cookie_or_cached_personal_response(self):
        views=Mock()
        views.read.return_value={'items':[]}
        app=MiniApp(views,TOKEN)
        status,headers,_=call(app,REQUEST_METHOD='POST')
        self.assertEqual(status,'405 Method Not Allowed')
        views.read.assert_not_called()
        status,headers,_=call(app)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Set-Cookie',headers)
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertIn("object-src 'none'",headers['Content-Security-Policy'])

    def test_internal_database_and_handler_errors_are_sanitized(self):
        for error,status in ((psycopg.OperationalError('db-secret'),'503'),(RuntimeError('internal-secret'),'500')):
            views=Mock()
            views.read.side_effect=error
            with self.assertLogs('app.web'):
                code,_,body=call(MiniApp(views,TOKEN))
            self.assertTrue(code.startswith(status))
            self.assertNotIn(b'secret',body)

    def test_static_page_and_unknown_route_do_not_touch_database(self):
        views=Mock()
        status,_,body=call(MiniApp(views,TOKEN),'/',raw='')
        self.assertEqual(status,'200 OK')
        self.assertIn('Подземелье знаний'.encode(),body)
        self.assertEqual(call(MiniApp(views,TOKEN),'/../../.env')[0],'404 Not Found')
        views.read.assert_not_called()

    def test_frontend_does_not_use_unsafe_identity_or_html_injection(self):
        from app.web import STATIC
        js=(STATIC/'app.js').read_text()
        self.assertNotIn('initDataUnsafe',js)
        self.assertNotIn('innerHTML',js)
        self.assertNotIn('localStorage',js)
        self.assertIn('textContent',js)

    def test_unregistered_user_never_reaches_personal_stores(self):
        views=ReadViews(Mock())
        views.users=Mock()
        views.users.registered_group.return_value=None
        with patch('app.web.HomeworkStore') as homework:
            result=views.read(10,'/api/homework')
        self.assertIsNone(result['group'])
        homework.assert_not_called()

    def test_web_entrypoint_is_disabled_without_explicit_mode(self):
        from mini_app import main
        with patch.dict(os.environ,{},clear=True),patch('mini_app.make_server') as serve:
            with self.assertRaises(ValueError):
                main()
            serve.assert_not_called()
