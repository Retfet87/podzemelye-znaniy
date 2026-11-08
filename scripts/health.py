"""Приватный статус: python -m scripts.health /private/path/health.json."""

import argparse
import json

from app.health import read_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument(
        "--max-age",
        type=float,
        default=120,
        help="Максимальный возраст MAX/DB snapshot, секунд; не performance SLA.",
    )
    args = parser.parse_args()
    try:
        status = read_status(args.file, max_age=args.max_age)
    except (OSError, ValueError, TypeError, KeyError):
        print("Health-файл недоступен или имеет неверный формат/права.")
        return 2
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0 if status["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
