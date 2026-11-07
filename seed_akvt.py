"""Compatibility entrypoint for the isolated demo seed. No production config."""
from seed_demo import main

if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
