"""Public GitHub witness: each signed tree head is committed as a JSON file.

Path: witness/<clinic_id>/<tree_size>.json in the witness repository. Only signed tree heads go
there (roots, sizes, signatures), never patient data.
"""

import base64
import json
from typing import Any

import httpx

API = "https://api.github.com"


class WitnessError(Exception):
    pass


class GitHubWitness:
    def __init__(
        self, repo: str, token: str, branch: str = "main", client: httpx.AsyncClient | None = None
    ) -> None:
        self.repo = repo
        self.token = token
        self.branch = branch
        self._client = client

    def path(self, clinic_id: str, tree_size: int) -> str:
        return f"witness/{clinic_id}/{tree_size}.json"

    async def publish(self, clinic_id: str, tree_size: int, document: dict[str, Any]) -> str:
        """Commit the document and return the commit sha. Publishing the same file twice is fine."""
        body = json.dumps(document, indent=2, sort_keys=True) + "\n"
        url = f"{API}/repos/{self.repo}/contents/{self.path(clinic_id, tree_size)}"
        headers = {
            "authorization": f"Bearer {self.token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
        }
        payload = {
            "message": f"Signed tree head {clinic_id} size {tree_size}",
            "content": base64.b64encode(body.encode()).decode(),
            "branch": self.branch,
        }
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.put(url, json=payload, headers=headers)
            if resp.status_code == 422:
                # Already there (a rerun). Return the existing file's last commit.
                existing = await client.get(url, headers=headers, params={"ref": self.branch})
                if existing.status_code == 200:
                    return str(existing.json()["sha"])
            if resp.status_code not in (200, 201):
                raise WitnessError(f"GitHub returned {resp.status_code}")
            return str(resp.json()["commit"]["sha"])
        finally:
            if self._client is None:
                await client.aclose()

    def commit_url(self, sha: str) -> str:
        return f"https://github.com/{self.repo}/commit/{sha}"
