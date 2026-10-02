"""Provenance for reproducible CLI reports; paths are repository relative."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def provenance(inputs: list[Path]) -> dict:
    paths = sorted(set(inputs + list((ROOT / "mpa").glob("*.py"))))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "simpy")},
        "hash_convention": "SHA256 of UTF-8 text with CRLF normalized to LF",
        "sha256": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for path in paths
        },
    }
