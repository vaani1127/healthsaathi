"""Run detection by hand: score today's accesses of every clinic, or fit the nightly models.

uv run python -m app.detect.job            # score now
uv run python -m app.detect.job --fit      # fit models on the last 30 days
"""

import argparse
import asyncio
import json
import sys

from app.core.db import get_engine
from app.detect import service


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fit", action="store_true", help="fit models instead of scoring")
    args = parser.parse_args(argv)
    try:
        result = await (service.fit_models() if args.fit else service.score_all())
    finally:
        await get_engine().dispose()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
