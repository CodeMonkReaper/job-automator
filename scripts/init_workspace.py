"""Script de inicialización rápida del workspace para Job Automator."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from job_automator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["init", *sys.argv[1:]]))
