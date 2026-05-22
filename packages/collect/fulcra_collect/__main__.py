"""Enable `python -m fulcra_collect` — used to spawn worker subprocesses
with a PATH-independent interpreter path."""
from fulcra_collect.cli import cli

if __name__ == "__main__":
    cli()
