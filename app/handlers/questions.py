"""Контекстные вопросы после команд, активных форм и основных разделов."""
from datetime import datetime, timedelta, time
from app.timeutil import SAMARA, format_deadline
from app.services.today import lessons, start_end
from app.handlers.base import Handler
from app.handlers.menus import HOME,MENU
from app.repositories.community import CommunityStore
from app.repositories.homework import HomeworkStore
from app.services.schedule_formatter import format_schedule,requested_days
from app.services.today import normalise


class QuestionsHandler(Handler):
    def on_message(self,ctx):
        if not ctx.private or not ctx.actor or not isinstance(ctx.text,str) or ctx.text.strip().startswith('/') or ctx.deps.today is None:
            return False
        group=ctx.users.registered_group(ctx.actor)
        if not group:
            return False
        text=normalise(ctx.text)
        buttons=HOME
        now = datetime.now(SAMARA)
        if text in ('что у меня сегодня','что сегодня','какие пары сегодня'):
            reply=ctx.deps.today.home(ctx.actor,group,now=now)
        elif text in ('какое расписание на следующую неделю','какие пары на следующей неделе'):
            rows=ctx.schedules.for_user(ctx.actor,requested_days('next_week',now.date()))
            reply=format_schedule(rows) if rows else 'Расписание пока не опубликовано.'
            buttons=MENU
        elif text in ('во сколько первая пара','когда первая пара'):
            rows=ctx.schedules.for_user(ctx.actor,[now.date()])
            current,uncertain=lessons(rows)
            if uncertain:
                reply='Тип недели не подтверждён. Открой расписание, чтобы сверить варианты.'
            elif current:
                first=min(current,key=lambda r:r[4])
                subjects=' / '.join(sorted({r[6] for r in current if r[4]==first[4]}))
                reply=f'Первая пара сегодня в {start_end(first)[0]:%H:%M}: {subjects}.'
            else:
                reply='Сегодня занятий нет.' if rows and rows[0][2] is not None else 'Расписание пока не опубликовано.'
        elif text in ('какие задания на завтра','что задали на завтра','задания на завтра'):
            start=datetime.combine(now.date()+timedelta(days=1),time.min,SAMARA)
            tasks=HomeworkStore(ctx.db,ctx.actor,None).due_list(start,start+timedelta(days=1))
            reply='\n'.join(f'{subject}: {body[:350]} · {format_deadline(deadline)}' for _,subject,body,deadline in tasks) or 'Заданий со сроком на завтра пока нет.'
        elif text in ('какие ближайшие мероприятия','ближайшие мероприятия','какие мероприятия'):
            rows=CommunityStore(ctx.db,ctx.actor,None).events(False,0)
            reply='\n'.join(f'{title} · {when.astimezone(SAMARA):%d.%m %H:%M}' for _,title,when,_ in rows) or 'Мероприятий пока нет.'
        elif text in ('что завтра','какие завтра пары','какие пары завтра','что у меня завтра'):
            rows=ctx.schedules.for_user(ctx.actor,requested_days('tomorrow'))
            reply=format_schedule(rows) if rows else 'Расписание на завтра не загружено.'
            buttons=MENU
        elif text in ('кто куратор','кто мой куратор','мой куратор'):
            store=CommunityStore(ctx.db,ctx.actor,None)
            cards=[]
            for identifier,_ in store.materials('curators',2**63-1)[:3]:
                record=store.material(identifier)
                if record:
                    cards.append(f'{record[1]}\n{record[2]}')
            reply='\n\n'.join(cards) if cards else 'Контактов куратора пока нет. Уточни их в учебном заведении.'
        elif text in ('что задали','какие задания','мои задания'):
            store=HomeworkStore(ctx.db,ctx.actor,None)
            cards=[]
            for shared in (False,True):
                for gid,_,_ in store.groups(shared):
                    for task,_,_ in store.page(gid,shared,2**63-1)[:3]:
                        record=store.get(task)
                        if record:
                            cards.append(f'{record[3]}: {record[4]}')
                    if not shared:
                        break
            reply='\n\n'.join(cards) if cards else 'Доступных заданий пока нет. Добавить личное можно в разделе «Домашние задания».'
        else:
            reply=ctx.deps.today.lesson_question(ctx.actor,ctx.text)
            if reply is None:
                reply='Могу показать расписание и задания. Например: «когда физика», «где математика», «что завтра» или «что задали».'
        ctx.client.send_message({'user_id':ctx.actor},reply[:3900],attachments=buttons)
        return True
