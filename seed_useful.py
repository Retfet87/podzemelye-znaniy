"""Заполнение раздела «Полезное» данными демо-сайта (только в изолированной DEMO_DATABASE_URL)."""
import argparse
from app.demo import demo_database
from app.demo_seed import clean_useful, seed_useful


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true', help='добавить 32 карточки с демо-сайта')
    mode.add_argument('--remove', action='store_true', help='удалить помеченные демо-карточки')
    args = parser.parse_args()
    db = demo_database()
    db.check_schema()
    with db.connect() as conn:
        result = seed_useful(conn) if args.apply else clean_useful(conn)
    print('ДЕМО: готово; рабочие данные не используются.', result or '')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None