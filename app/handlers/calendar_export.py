"""Calendar buttons always resolve the authenticated actor's own group."""
from datetime import date, timedelta

from app.handlers.base import Handler
from app.max_api.keyboards import callback_button, inline_keyboard
from app.repositories.community import CommunityStore
from app.services.calendar_export import event_calendar, schedule_calendar
from app.services.schedule_formatter import requested_days


class CalendarHandler(Handler):
    def on_callback(self, ctx):
        if not ctx.payload.startswith('calendar:'):
            return False
        if not ctx.private or not ctx.actor:
            ctx.client.answer(ctx.callback_id, notification='Открой календарь в личном диалоге.')
            return True
        try:
            action = ctx.payload.removeprefix('calendar:')
            if action == 'week':
                monday = requested_days('week')[0]
                self.weeks(ctx, monday)
                return True
            if action.startswith(('choose:', 'export:')):
                operation, value = action.split(':', 1)
                monday = date.fromisoformat(value)
                if monday.weekday() or abs((monday - requested_days('week')[0]).days) > 364:
                    raise ValueError('Выбери неделю в пределах года.')
                if operation == 'choose':
                    self.weeks(ctx, monday)
                    return True
                rows = ctx.schedules.for_user(ctx.actor, [monday + timedelta(days=i) for i in range(7)])
                if not rows:
                    raise ValueError('Сначала выбери группу через /start.')
                data, name = schedule_calendar(rows), f'schedule-{monday}.ics'
            elif action.startswith('event:'):
                from app.handlers.common import number
                event = number(action.removeprefix('event:'))
                row = CommunityStore(ctx.db, ctx.actor, ctx.admin).event(event)
                if not row:
                    raise ValueError('Мероприятие недоступно.')
                data, name = event_calendar(event, row), f'event-{event}.ics'
            else:
                raise ValueError('Кнопка календаря устарела.')
            attachment = ctx.client.upload_file(name, data)
            ctx.client.send_message({'user_id': ctx.actor}, 'Импортируй файл в календарь. Это снимок; после изменений скачай заново.', attachments=[attachment])
            ctx.client.answer(ctx.callback_id, notification='Календарь отправлен.')
        except ValueError as error:
            ctx.client.answer(ctx.callback_id, notification=str(error))
        return True

    @staticmethod
    def weeks(ctx, monday):
        ctx.client.answer(ctx.callback_id, text=f'Неделя {monday:%d.%m.%Y} — {monday + timedelta(days=6):%d.%m.%Y}', attachments=inline_keyboard([
            [callback_button('← Неделя', f'calendar:choose:{monday - timedelta(days=7)}'),
             callback_button('Неделя →', f'calendar:choose:{monday + timedelta(days=7)}')],
            [callback_button('📆 В календарь', f'calendar:export:{monday}')]]))
