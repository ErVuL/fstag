"""Entry point for fstag."""

import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="fstag",
        description="Tag files with colored tags and browse them in a file-system UI.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to manage (default: current directory)",
    )
    args = parser.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: '{root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    from .store import TagStore
    from .ui import App

    store = TagStore(root)
    app = App(store)
    app.run()


if __name__ == "__main__":
    main()
