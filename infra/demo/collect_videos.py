"""Copy one video per flow test from a Playwright JSON report, named after the flow.

python infra/demo/collect_videos.py demo-videos/report.json demo-videos
"""

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# Test title pattern to file name, in demo order.
FLOWS = [
    (r"^flow 1:", "flow-1-reception-registers-books-token"),
    (r"^flow 2:", "flow-2-nurse-records-vitals"),
    (r"^flow 3:", "flow-3-doctor-note-prescription-lab-follow-up"),
    (r"^flow 4:", "flow-4-lab-result"),
    (r"^flow 5:", "flow-5-billing-receipt"),
    (r"^flow 6:", "flow-6-patient-access-log-and-report"),
    (r"receipt against the local chain", "flow-6b-patient-verifies-receipt-on-chain"),
    (r"^flow 7: doctor", "flow-7a-doctor-break-glass"),
    (r"^flow 7: admin", "flow-7b-admin-reviews-break-glass"),
    (r"^flow 8: a doctor", "flow-8a-doctor-typed-reason"),
    (r"^flow 8: admin", "flow-8b-admin-reviews-alert"),
    (r"^flow 9:", "flow-9-audit-page"),
]


def videos(suite: dict[str, Any]) -> list[tuple[str, str]]:
    found = []
    for spec in suite.get("specs", []):
        for t in spec.get("tests", []):
            for result in t.get("results", []):
                for a in result.get("attachments", []):
                    if a.get("name") == "video" and a.get("path"):
                        found.append((spec["title"], a["path"]))
    for child in suite.get("suites", []):
        found += videos(child)
    return found


def main(report: str, target: str) -> int:
    data = json.loads(Path(report).read_text(encoding="utf-8"))
    recorded = [v for s in data.get("suites", []) for v in videos(s)]
    out = Path(target)
    missing = []
    for pattern, name in FLOWS:
        match = next((p for title, p in recorded if re.search(pattern, title)), None)
        if match is None:
            missing.append(name)
            continue
        shutil.copyfile(match, out / f"{name}.webm")
    print(
        f"{len(FLOWS) - len(missing)} videos in {out}" + (f"; missing {missing}" if missing else "")
    )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
