"""Personal context built from permitted records. No LLM or external requests."""
from datetime import datetime,time,timedelta
import re

from app.repositories.homework import HomeworkStore
from app.services.schedule_formatter import BELLS
from app.timeutil import SAMARA


def normalise(text):
    return ' '.join(text.casefold().replace('ё','е').strip(' ?!.').split())


def lessons(rows):
    """Resolve only confirmed timetable variants; never guess unknown parity."""
    selected=[]
    uncertain=False
    for row in rows:
        day,_,version,anchor,pair,parity,*_=row
        if version is None or pair is None:
            continue
        if parity!='all':
            if anchor is None:
                uncertain=True
                continue
            current='denominator' if ((day-anchor).days//7)%2 else 'numerator'
            if parity!=current:
                continue
        selected.append(row)
    return selected,uncertain


def start_end(row):
    start,end=BELLS[row[4]-1].split('–')
    return tuple(datetime.combine(row[0],time.fromisoformat(value),SAMARA) for value in (start,end))


def schedule_today(rows,now):
    if not rows or rows[0][2] is None:
        return 'Расписание на сегодня не загружено.'
    current,uncertain=lessons(rows)
    if uncertain:
        return 'Тип недели не подтверждён. Открой расписание, чтобы сверить варианты.'
    if not current:
        return 'Сегодня занятий нет.'
    summary=f'Сегодня: {len(current)} пар.'
    for row in current:
        start,end=start_end(row)
        if end<=now:
            continue
        subject,room=row[6][:180],row[8][:70] or 'кабинет не указан'
        if start<=now:
            return f'{summary}\nСейчас: {subject} · {room}, до {end:%H:%M}.'
        return f'{summary}\nБлижайшая: {subject} · {room}, в {start:%H:%M}.'
    return summary+' Занятия закончились.'


class TodayService:
    def __init__(self,db,schedules):
        self.db,self.schedules=db,schedules

    def home(self,actor,group,*,now=None):
        now=(now or datetime.now(SAMARA)).astimezone(SAMARA)
        rows=self.schedules.for_user(actor,[now.date()])
        until=datetime.combine(now.date()+timedelta(days=2),time.min,SAMARA)
        count=HomeworkStore(self.db,actor,None).due_count(now,until)
        due=f'Сегодня и завтра: {count} заданий со сроком.' if count else 'Сроков заданий на сегодня и завтра нет.'
        return f'🏰 Подземелье знаний\nТвоя группа: {group}\n\n{schedule_today(rows,now)}\n{due}'

    def lesson_question(self,actor,text,*,now=None):
        match=re.fullmatch(r'(когда|во сколько|где|какой кабинет)\s+(?:у меня\s+)?(.+)',normalise(text))
        if not match:
            return None
        intent,subject=match.groups()
        subject=re.sub(r'^(?:кабинет|пара по)\s+','',subject)
        if len(subject)<2:
            return 'Укажи предмет, например: «когда физика».'
        now=(now or datetime.now(SAMARA)).astimezone(SAMARA)
        rows=self.schedules.for_user(actor,[now.date()+timedelta(days=i) for i in range(14)])
        current,uncertain=lessons(rows)
        if any(r[4] is not None and r[3] is None and r[5] != 'all'
               and subject in normalise(r[6]) and start_end(r)[1] > now for r in rows):
            return 'Тип недели не подтверждён. Открой расписание, чтобы сверить варианты предмета.'
        hits=[r for r in current if subject in normalise(r[6]) and start_end(r)[1]>now]
        names=sorted({r[6] for r in hits})
        if len(names)>1:
            return 'Уточни предмет: '+', '.join(names[:5])+'.'
        if not hits:
            extra=' Тип недели не подтверждён.' if uncertain else ''
            return 'В загруженном расписании на ближайшие 14 дней такого предмета не нашлось.'+extra+' Проверь название и открой расписание.'
        row=hits[0]
        start,_=start_end(row)
        return f'{row[6][:180]} · {start:%d.%m в %H:%M}\nКабинет: {row[8][:70] or "не указан"}.'
