"""Чтение четырёхстрочной сетки расписания АКВТ из BIFF XLS."""
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import xlrd

DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота")
SLOTS = ("1-2", "3-4", "5-6", "7-8", "9-10", "11-12")
GROUP = re.compile(r"[А-ЯЁA-Z]+-\d+")
TEACHER = re.compile(r"[А-ЯЁ][а-яё]+(?:[- ][А-ЯЁ][а-яё]+)*\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?")
ROOM = re.compile(r"ауд\.?\s*\d+[а-яёa-z]?", re.IGNORECASE)
GRAY_INDEXES = {22, 23, 47, 55, 63}


@dataclass(frozen=True)
class Lesson:
    group: str
    weekday: int
    pair: int
    parity: str
    subject: str
    teacher: str
    room: str
    note: str
    raw: tuple[str, ...]


def clean(value):
    return " ".join(str(value).split())


def is_split_by_content(values):
    """Определяет разделение на 1-ю и 2-ю неделю по структуре строк, если граница не задана."""
    if not (values[0] and values[2]):
        return False
    if TEACHER.match(values[2]) or ROOM.match(values[2]):
        return False
    has_upper_details = bool(TEACHER.search(values[1]) or ROOM.search(values[1]))
    has_lower_details = bool(TEACHER.search(values[3]) or ROOM.search(values[3]))
    return has_upper_details or has_lower_details


def parse_block(values, split, gray_flags, group, weekday, pair):
    parts = (
        (("numerator", values[:2], any(gray_flags[:2])),
         ("denominator", values[2:], any(gray_flags[2:])))
        if split else
        (("all", values, any(gray_flags)),)
    )
    for parity, rows, is_other_building in parts:
        if not any(rows):
            continue
        subject, *details = rows
        teachers, rooms, notes = [], [], []
        if not subject or TEACHER.match(subject) or ROOM.match(subject):
            details = list(rows)
            subject = "Предмет не указан"
        for detail in filter(None, details):
            teacher = TEACHER.match(detail)
            if teacher:
                teachers.append(teacher[0])
                detail = detail[teacher.end():].strip()
            room = ROOM.match(detail)
            if room:
                rooms.append(room[0])
                detail = detail[room.end():].strip()
            if detail:
                notes.append(detail)
        if is_other_building and not any("корпус" in n.lower() for n in notes):
            notes.append("Другой корпус")
        yield Lesson(group, weekday, pair, parity, subject,
                     " / ".join(teachers), " / ".join(rooms), " / ".join(notes),
                     tuple(rows))


def parse_xls(path):
    with Path(path).open("rb") as file:
        source = file.read(8 * 1024 * 1024 + 1)
    if len(source) > 8 * 1024 * 1024:
        raise ValueError("XLS больше 8 МБ.")
    try:
        book = xlrd.open_workbook(file_contents=source, formatting_info=True)
    except (xlrd.XLRDError, ValueError) as error:
        raise ValueError("Файл не удалось прочитать как XLS.") from error
    if book.nsheets != 1:
        raise ValueError("Ожидался один лист сетки АКВТ.")
    sheet = book.sheet_by_index(0)
    merged = {}
    for top, bottom, left, right in sheet.merged_cells:
        for row in range(top, bottom):
            for col in range(left, right):
                merged[row, col] = (top, left)

    def value(row, col):
        row, col = merged.get((row, col), (row, col))
        if sheet.cell_type(row, col) == xlrd.XL_CELL_ERROR:
            raise ValueError("В сетке XLS есть ячейка с ошибкой Excel.")
        text = clean(sheet.cell_value(row, col))
        if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in text):
            raise ValueError("В XLS есть недопустимый управляющий символ.")
        return text

    def separator(row, col):
        # Если строка row+1 и row+2 не объединены вертикально и между ними есть любая линия границы
        if merged.get((row + 1, col)) == merged.get((row + 2, col)) and (row + 2, col) in merged:
            return False
        upper = book.xf_list[sheet.cell_xf_index(row + 1, col)].border
        lower = book.xf_list[sheet.cell_xf_index(row + 2, col)].border
        return upper.bottom_line_style > 0 or lower.top_line_style > 0

    def is_gray(row, col):
        row, col = merged.get((row, col), (row, col))
        xf = book.xf_list[sheet.cell_xf_index(row, col)]
        bg = xf.background
        if not bg or bg.fill_pattern == 0:
            return False
        for idx in (bg.pattern_colour_index, bg.background_colour_index):
            if idx in GRAY_INDEXES:
                return True
            rgb = book.colour_map.get(idx)
            if rgb and len(rgb) == 3 and all(isinstance(c, int) for c in rgb):
                r, g, b = rgb
                # Любой оттенок серого (не чисто белый и не чисто чёрный)
                if max(r, g, b) - min(r, g, b) <= 18 and 50 <= r <= 242:
                    return True
        return False

    headers = [r for r in range(sheet.nrows)
               if (value(r, 0), value(r, 1)) == ("Дни", "Часы")]
    if not headers:
        raise ValueError("Не найдена строка заголовков «Дни / Часы».")
    header = headers[0]
    groups = {c: value(header, c) for c in range(2, sheet.ncols) if value(header, c)}
    if not groups or len(set(groups.values())) != len(groups) or not all(
        GROUP.fullmatch(name) for name in groups.values()
    ):
        raise ValueError("Не удалось однозначно прочитать заголовки групп.")
    slots = [r for r in range(header + 1, sheet.nrows) if value(r, 1) in SLOTS]
    if [value(r, 1) for r in slots] != list(SLOTS) * len(DAYS):
        raise ValueError("Ожидалось шесть блоков по шесть пар, с понедельника по субботу.")
    if slots[-1] + 4 > sheet.nrows or any(
        later - earlier < 4 for earlier, later in zip(slots, slots[1:])
    ):
        raise ValueError("Блоки пар пересекаются или обрываются.")
    lessons = []
    used_rows = set(headers)
    for weekday, day in enumerate(DAYS):
        day_slots = slots[weekday * len(SLOTS):(weekday + 1) * len(SLOTS)]
        if "".join(value(r, 0) for r in range(day_slots[0], day_slots[-1] + 4)) != day:
            raise ValueError(f"Не совпала подпись дня «{day}».")
        for pair, row in enumerate(day_slots, 1):
            used_rows.update(range(row, row + 4))
            for col, name in groups.items():
                values = [value(r, col) for r in range(row, row + 4)]
                gray_flags = [is_gray(r, col) for r in range(row, row + 4)]
                split = separator(row, col) or is_split_by_content(values)
                lessons.extend(parse_block(values, split, gray_flags, name, weekday, pair))
    for r in range(header + 1, sheet.nrows):
        for c in range(2, sheet.ncols):
            if value(r, c) and (r not in used_rows or c not in groups):
                raise ValueError(f"Данные вне сетки, строка {r + 1}, столбец {c + 1}.")
    if not lessons:
        raise ValueError("В файле нет занятий.")
    return sha256(source).hexdigest(), tuple(groups.values()), lessons


def print_summary(groups, lessons):
    counts = Counter(lesson.group for lesson in lessons)
    for name in groups:
        print(f"{name}: {counts[name]}")
    print(f"Групп: {len(groups)}, записей: {len(lessons)}.")
    print("Числитель / знаменатель / обе недели: " + " / ".join(
        str(sum(lesson.parity == parity for lesson in lessons))
        for parity in ("numerator", "denominator", "all")
    ))
    for lesson in lessons:
        if lesson.note or lesson.subject == "Предмет не указан":
            print(f"{lesson.group}, {DAYS[lesson.weekday]}, пара {lesson.pair}, "
                  f"{lesson.parity}: {' | '.join(filter(None, lesson.raw))}")