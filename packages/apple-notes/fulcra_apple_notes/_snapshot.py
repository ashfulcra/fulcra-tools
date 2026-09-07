"""Bounded by a parent process; SQLite online backup keeps WAL reads consistent."""
from pathlib import Path
import sqlite3
import sys


def main(args=None):
    source, destination = args if args is not None else sys.argv[1:]
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True,
                         timeout=2) as reader:
        with sqlite3.connect(destination) as writer:
            reader.backup(writer, pages=256, sleep=0.05)


if __name__ == '__main__':
    main()
