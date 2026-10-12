"""Local witness: a git repository in a folder, for the local demo and for tests.

It does what the GitHub witness does (one JSON file per signed tree head at
witness/<clinic_id>/<tree_size>.json, one commit each) without a network or a token, so its
receipts are stored under the same `github` backend with the commit hash. Only signed tree heads
go there, never patient data.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

GIT_IDENTITY = (
    "-c",
    "user.name=HealthSaathi witness",
    "-c",
    "user.email=witness@healthsaathi.test",
)


class LocalWitnessError(Exception):
    pass


class LocalWitness:
    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)

    def path(self, clinic_id: str, tree_size: int) -> str:
        return f"witness/{clinic_id}/{tree_size}.json"

    def _git(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed git arguments, no shell
            ["git", "-c", "safe.directory=*", *GIT_IDENTITY, "-C", str(self.folder), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise LocalWitnessError(result.stderr.strip() or f"git {args[0]} failed")
        return result.stdout.strip()

    def _publish(self, clinic_id: str, tree_size: int, document: dict[str, Any]) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        if not (self.folder / ".git").exists():
            self._git("init", "--quiet", "--initial-branch", "main")
        relative = self.path(clinic_id, tree_size)
        target = self.folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if target.exists() and target.read_text(encoding="utf-8") == body:
            # Already published by an earlier run: return the commit that added it.
            return self._git("log", "-n", "1", "--format=%H", "--", relative)
        target.write_bytes(body.encode())
        self._git("add", relative)
        self._git("commit", "--quiet", "-m", f"Signed tree head {clinic_id} size {tree_size}")
        return self._git("rev-parse", "HEAD")

    async def publish(self, clinic_id: str, tree_size: int, document: dict[str, Any]) -> str:
        """Commit the document and return the commit hash. Publishing twice is fine."""
        return await asyncio.to_thread(self._publish, clinic_id, tree_size, document)
