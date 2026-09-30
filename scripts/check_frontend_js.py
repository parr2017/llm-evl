"""Syntax-check the inline <script> of the web UI.

The UI is a single hand-edited HTML file with no build step, so a stray
newline inside a JS string literal only shows up as "Invalid or unexpected
token" in the browser console — after the app has already failed to mount.
This turns that into a fast local failure.

    .venv/Scripts/python.exe scripts/check_frontend_js.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = [ROOT / "llm_evl" / "web" / "index.html", ROOT / "llm_evl" / "web" / "login.html"]


def check(path: Path) -> list[str]:
    """Return a list of problems for one HTML file (empty means fine)."""
    src = path.read_text(encoding="utf-8")
    problems: list[str] = []
    node = shutil.which("node")
    for i, block in enumerate(re.findall(r"<script>(.*?)</script>", src, re.S)):
        if not block.strip():
            continue
        if not node:
            problems.append(
                f"{path.name} block {i}: node not found, cannot syntax-check. "
                f"Install Node.js or check the file by hand."
            )
            continue
        tmp = Path(tempfile.gettempdir()) / f"{path.stem}_block{i}.js"
        tmp.write_text(block, encoding="utf-8")
        try:
            r = subprocess.run([node, "--check", str(tmp)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                problems.append(f"{path.name} block {i}:\n{r.stderr.strip()[:1200]}")
        finally:
            tmp.unlink(missing_ok=True)
    return problems


def main() -> int:
    all_problems: list[str] = []
    for f in FILES:
        if not f.exists():
            continue
        problems = check(f)
        status = "OK" if not problems else f"{len(problems)} problem(s)"
        print(f"{f.name}: {status}")
        all_problems.extend(problems)
    if all_problems:
        print("\n" + "\n".join(all_problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
