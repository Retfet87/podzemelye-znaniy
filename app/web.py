"""Small read-only WSGI interface. Every API request verifies signed MAX identity."""
import json
import logging
from pathlib import Path

import psycopg

from app.repositories.users import UserRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.homework import HomeworkStore
from app.repositories.community import CommunityStore
from app.services.schedule_formatter import format_schedule, requested_days
from app.timeutil import format_deadline
from app.web_auth import InvalidLaunch, verify_init_data

logger=logging.getLogger(__name__)
STATIC=Path(__file__).resolve().parents[1]/'web'
ASSETS={'/':('index.html','text/html; charset=utf-8'),
        '/app.js':('app.js','text/javascript; charset=utf-8'),
        '/style.css':('style.css','text/css; charset=utf-8')}
ENDPOINTS={'/api/schedule','/api/homework','/api/events'}


class ReadViews:
    def __init__(self,db):
        self.db=db
        self.users=UserRepository(db)

    def read(self,actor,path):
        group=self.users.registered_group(actor)
        if not group:
            return {'group':None,'items':['Сначала выбери группу в личном чате с ботом.']}
        items=[]
        if path=='/api/schedule':
            rows=ScheduleRepository(self.db).for_user(actor,requested_days('week'))
            items=[format_schedule(rows)] if rows else ['Расписание пока не загружено.']
        elif path=='/api/homework':
            store=HomeworkStore(self.db,actor,None)
            for shared in (True,False):
                for gid,_,_ in store.groups(shared):
                    for task,_,_ in store.page(gid,shared,2**63-1)[:8]:
                        record=store.get(task)
                        if record:
                            _,_,common,subject,body,deadline,*_=record
                            items.append(f"{'Общее' if common else 'Личное'} · {subject}\n{body}\nСрок: {format_deadline(deadline) if deadline else 'не указан'}")
                    if not shared:
                        break  # personal page already spans all permitted personal groups
        elif path=='/api/events':
            store=CommunityStore(self.db,actor,None)
            for identifier,_,_,_ in store.events(False,0)[:8]:
                record=store.event(identifier)
                if record:
                    title,body,starts,place,*_=record
                    items.append(f'{title}\n{format_deadline(starts)} · {place or "Место не указано"}\n{body}')
        return {'group':group,'items':items or ['Пока ничего нет. Новые записи появятся здесь после публикации.']}


class MiniApp:
    def __init__(self,views,bot_token,*,demo_policy=None):
        self.views,self.bot_token,self.demo_policy=views,bot_token,demo_policy

    def __call__(self,environ,start_response):
        headers=[('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),
                 ('Referrer-Policy','no-referrer'),
                 ('Content-Security-Policy',"default-src 'self'; script-src 'self' https://st.max.ru; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'")]
        status='200 OK'
        kind='application/json; charset=utf-8'
        try:
            path=environ.get('PATH_INFO','/')
            if environ.get('REQUEST_METHOD')!='GET':
                status='405 Method Not Allowed'; result={'error':'Доступно только чтение.'}
            elif environ.get('QUERY_STRING'):
                status='400 Bad Request'; result={'error':'Параметры запроса не поддерживаются.'}
            elif path in ASSETS:
                filename,kind=ASSETS[path]
                result=(STATIC/filename).read_bytes()
            elif path not in ENDPOINTS:
                status='404 Not Found'; result={'error':'Раздел не найден.'}
            else:
                actor=verify_init_data(environ.get('HTTP_X_MAX_INIT_DATA',''),self.bot_token)
                if self.demo_policy is not None and not self.demo_policy.permits(actor):
                    status='403 Forbidden'; result={'error':'ДЕМО выключено или доступ запрещён.'}
                else:
                    result=self.views.read(actor,path)
                    # Recheck hot switch before emitting already queried personal data.
                    if self.demo_policy is not None:
                        if not self.demo_policy.permits(actor):
                            status='403 Forbidden'; result={'error':'ДЕМО выключено.'}
                        else:
                            result=result|{'demo':True}
        except InvalidLaunch:
            status='401 Unauthorized'; result={'error':'Открой приложение заново из MAX.'}
        except psycopg.Error:
            logger.warning('Mini-app database unavailable')
            status='503 Service Unavailable'; result={'error':'Данные временно недоступны. Попробуй позже.'}
        except Exception:
            logger.exception('Mini-app request failed')
            status='500 Internal Server Error'; result={'error':'Не удалось открыть данные. Попробуй позже.'}
        body=result if isinstance(result,bytes) else json.dumps(result,ensure_ascii=False).encode()
        start_response(status,headers+[('Content-Type',kind),('Content-Length',str(len(body)))])
        return [body]
