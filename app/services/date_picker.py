"""Календарь формы: состояние сохраняется вместе с черновиком, без публикации."""
import calendar
from datetime import date, datetime, timedelta

from app.timeutil import SAMARA
from app.max_api.keyboards import inline_keyboard, callback_button

def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError('Такой даты нет. Выбери день в календаре.') from None


MONTHS = ('Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь')


def keyboard(draft, prefix, *, optional=False):
    now = datetime.now(SAMARA)
    cursor = draft.date_cursor
    if draft.date_value:
        text = f'Дата: {date.fromisoformat(draft.date_value):%d.%m.%Y}. Выбери время (Астрахань).'
        rows = [[('09:00','dt:time:09:00'),('12:00','dt:time:12:00'),('15:00','dt:time:15:00')],
                [('18:00','dt:time:18:00'),('21:00','dt:time:21:00')],
                [('Другое время','dt:clock'),('Изменить дату','dt:reset')]]
        if cursor == 'clock':
            text += '\nОтправь только время, например 16:30.'
    elif cursor and cursor != 'manual':
        month = date.fromisoformat(cursor+'-01')
        text = f'{MONTHS[month.month-1]} {month.year} · выбери день (Астрахань).'
        rows = [[(str(day),f'dt:day:{month.year:04}-{month.month:02}-{day:02}') for day in week if day]
                for week in calendar.monthcalendar(month.year,month.month)]
        rows = [row for row in rows if row]
        prev = month-timedelta(days=1)
        following = (month.replace(day=28)+timedelta(days=4)).replace(day=1)
        rows += [[('Предыдущий месяц',f'dt:month:{prev:%Y-%m}'),('Следующий месяц',f'dt:month:{following:%Y-%m}')],
                 [('Быстрые даты','dt:reset'),('Ввести вручную','dt:manual')]]
    else:
        text = 'Выбери дату (Астрахань).'
        rows = [[('Сегодня','dt:offset:0'),('Завтра','dt:offset:1')],
                [('Послезавтра','dt:offset:2'),('Через неделю','dt:offset:7')],
                [('Другая дата',f'dt:month:{now:%Y-%m}'),('Ввести вручную','dt:manual')]]
        if cursor == 'manual':
            text += '\nМожно отправить дату и время: ДД.ММ.ГГГГ ЧЧ:ММ.'
    if getattr(draft,'task',None) is not None or getattr(draft,'id',None) is not None:
        rows.append([('Оставить значение','keep')])
    if optional:
        rows.append([('Без срока','skip')])
    rows += [[('Назад','prev'),('Отменить действие','cancel')]]
    built = [[callback_button(label,prefix+draft.button(label,action)[1]) for label,action in row] for row in rows]
    return text, inline_keyboard(built)


def choose(draft, action, *, text='', now=None):
    """Возвращает (готово, дата); ошибки не изменяют выбранный срок."""
    now = (now or datetime.now(SAMARA)).astimezone(SAMARA)
    if action is None and draft.date_value and draft.date_cursor == 'clock':
        action = 'dt:time:'+text
    if not action or not action.startswith('dt:'):
        return None
    kind, _, value = action[3:].partition(':')
    if kind == 'offset' and value in ('0','1','2','7'):
        draft.date_value = (now.date()+timedelta(days=int(value))).isoformat()
        draft.date_cursor = ''
    elif kind == 'month':
        month = parse_date(value+'-01')
        if not 1900 <= month.year <= 2100:
            raise ValueError('Выбери год от 1900 до 2100.')
        draft.date_cursor,draft.date_value = value,''
    elif kind == 'day':
        selected = parse_date(value)
        if not 1900 <= selected.year <= 2100 or selected < now.date():
            raise ValueError('Выбери сегодняшнюю или будущую дату.')
        draft.date_value,draft.date_cursor = selected.isoformat(),''
    elif kind == 'time' and draft.date_value:
        try:
            clock = datetime.strptime(value,'%H:%M').time()
        except ValueError:
            raise ValueError('Выбери время кнопкой или введи, например, 16:30.') from None
        result = datetime.combine(date.fromisoformat(draft.date_value),clock,SAMARA)
        if result <= now:
            raise ValueError('Это время уже прошло. Выбери более позднее время или другую дату.')
        draft.date_cursor,draft.date_value = '',''
        return True,result
    elif kind == 'clock' and draft.date_value:
        draft.date_cursor = 'clock'
    elif kind in ('reset','manual'):
        draft.date_cursor,draft.date_value = ('manual' if kind=='manual' else ''),''
    else:
        raise ValueError('Кнопка даты недействительна. Открой текущую форму.')
    return False,None
