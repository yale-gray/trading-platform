"""``python -m tradebot.runner`` delegates to the unified CLI (``tradebot runner start``)."""

import sys


def _main() -> int:
    from tradebot.cli import main

    return main(["runner", "start", *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(_main())
