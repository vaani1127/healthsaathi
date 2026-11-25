"""Run detection by hand: score today's accesses of every clinic, fit the nightly models, or
make the final alert lists of the days that are due.

uv run python -m app.detect.job            # score now (provisional alerts)
uv run python -m app.detect.job --fit      # fit the provisional models on the last 30 days
uv run python -m app.detect.job --daily    # final lists of due days (safe to repeat)
uv run python -m app.detect.job --daily --catch-up 400   # first run: finalise the history
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any

from app.core.db import get_engine
from app.detect import daily, service


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fit", action="store_true", help="fit models instead of scoring")
    parser.add_argument("--daily", action="store_true", help="final lists of the due days")
    parser.add_argument(
        "--catch-up",
        type=int,
        default=None,
        help="with --daily: due days that may still be finalised (default DETECT_CATCH_UP_DAYS)",
    )
    args = parser.parse_args(argv)
    result: Any
    try:
        if args.daily:
            runs = await daily.run_due(catch_up=args.catch_up)
            result = (
                {"busy": "another daily run holds the lock"}
                if runs is None
                else {c: [asdict(r) for r in days] for c, days in runs.items()}
            )
        else:
            result = await (service.fit_models() if args.fit else service.score_all())
    finally:
        await get_engine().dispose()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
