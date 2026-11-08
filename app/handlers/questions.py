"""Small explicit intent rules after commands, active forms and section triggers."""
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
        if text in ('что завтра','какие завтра пары','что у меня завтра'):
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
