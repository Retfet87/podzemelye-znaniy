"""Build only public synthetic assets. Never reads config, tokens or databases."""
import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import shutil

from app.demo_site_data import FORBIDDEN, ROOT, SITE_JSON, load_site

SOURCE = ROOT / 'demo-site'
FILES = ('index.html', 'app.js', 'style.css', 'CREDITS.md', 'assets/icon.svg',
         'assets/dungeon.png', 'assets/demo-flower.mp4', 'assets/demo-task.txt')


def dataset():
    # Keep module-level json.loads hookable for tests/test_final_products.py
    data = json.loads(SITE_JSON.read_text(encoding='utf-8'))
    if data != load_site():
        # Re-validate patched data when unit tests mock json.loads
        import re
        def require(condition, message='Invalid public demo fixture'):
            if not condition:
                raise ValueError(message)
        require(data['demo'] is True and set(data['sections']) == {'schedule', 'homework', 'events', 'useful'})
        def walk(value):
            if isinstance(value, dict):
                require(not set(value) & FORBIDDEN, 'Private fields in public fixture')
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                require('postgresql://' not in value and 'X-Max-Init-Data' not in value)
                require(not re.search('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]', value), 'Decorative emoji in public fixture')
        walk(data)
        for section in data['sections'].values():
            require(section['demo'] is True and bool(section['cards']))
            for card in section['cards'] + section.get('history', []):
                require(card['title'].startswith('ДЕМО'))
                for media in card.get('media', []):
                    require(media['src'] in ('./assets/dungeon.png', './assets/demo-flower.mp4'))
        require(len(data['sections']['useful']['cards']) == 32)
    return data


def build(destination=None):
    target = Path(destination or ROOT / 'dist').resolve()
    if target == ROOT or SOURCE == target or SOURCE in target.parents or target in ROOT.parents:
        raise ValueError('Choose a separate build output directory')
    allowed = set(FILES) | {'data/demo.json', 'assets/demo-week.ics'}
    if target.exists():
        if any(p.is_symlink() for p in target.rglob("*")):
            raise ValueError("Symlinks are forbidden in build output")
        extras = [p for p in target.rglob('*') if p.is_file() and p.relative_to(target).as_posix() not in allowed]
        if extras:
            raise ValueError('Build directory contains unknown files; use a new empty directory')
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        src, dest = SOURCE / name, target / name
        if src.is_symlink() or dest.is_symlink():
            raise ValueError('Symlinks are forbidden in public build')
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
    (target / 'data').mkdir(exist_ok=True)
    (target / 'data/demo.json').write_text(json.dumps(dataset(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    from app.services.calendar_export import schedule_calendar
    from app.timeutil import SAMARA
    monday = datetime(2026, 11, 9, tzinfo=SAMARA).date()
    rows = [
        (monday + timedelta(days=i), 'ДЕМО-11', 1, monday, lesson['pair'], 'all', lesson['subject'], lesson['teacher'], lesson['room'], 'Не официальное расписание')
        for i, card in enumerate(dataset()['sections']['schedule']['cards'])
        for lesson in card['lessons']
    ]
    (target / 'assets/demo-week.ics').write_bytes(schedule_calendar(rows, stamp=datetime(2026, 11, 8, tzinfo=UTC)))
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    print('Public static demo ready:', build(args.output))


if __name__ == '__main__':
    main()