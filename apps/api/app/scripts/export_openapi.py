"""Write the OpenAPI document used to generate the web app's typed client.

uv run python -m app.scripts.export_openapi apps/web/src/lib/api/openapi.json
"""

import json
import sys
from pathlib import Path

from app.main import create_app


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.scripts.export_openapi <output.json>")
        return 2
    Path(argv[1]).write_text(render(), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
