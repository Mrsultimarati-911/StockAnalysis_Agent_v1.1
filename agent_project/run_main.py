"""Run the adjacent configured stock Agent, independently of the current directory."""
from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    root = str(PROJECT_ROOT)
    if not sys.path or sys.path[0] != root:
        sys.path.insert(0, root)
    from src.cli import main as cli_main

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["run"]
    elif arguments[0] in {"--check", "--doctor"}:
        arguments = ["doctor", *arguments[1:]]
    elif arguments[0] == "--config" or arguments[0].startswith("--config="):
        arguments = ["run", *arguments]
    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
