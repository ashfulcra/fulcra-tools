"""Module entry point, including the isolated no-UI credential reader."""
import sys

if __name__ == "__main__":
    if sys.argv[1:] == ["_keychain-read"]:
        from .macos_keychain import child_main
        raise SystemExit(child_main())
    from fulcra_collect.cli import cli
    cli()
