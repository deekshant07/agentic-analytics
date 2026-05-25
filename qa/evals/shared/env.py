"""Shared .env loading for eval tools (benchmark + dashboard)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def load_project_dotenv(project_root: Path) -> tuple[list[str], str]:
    """
    Load variables from disk into os.environ.

    Order: ``<root>/.env.local`` → ``<root>/.env`` → ``find_dotenv()`` from cwd.
    Uses ``override=True`` so a valid repo ``.env`` replaces a bad shell export.
    Strips whitespace and optional wrapping quotes on ``OPENAI_API_KEY``.
    """
    loaded: list[str] = []
    for name in (".env.local", ".env"):
        p = project_root / name
        if p.is_file():
            load_dotenv(p, override=True)
            loaded.append(str(p))
            break
    if not loaded:
        found = find_dotenv(usecwd=True)
        if found:
            load_dotenv(found, override=True)
            loaded.append(found)

    raw = os.environ.get("OPENAI_API_KEY", "")
    if raw:
        cleaned = raw.strip().strip('"').strip("'")
        if cleaned != raw:
            os.environ["OPENAI_API_KEY"] = cleaned

    parts: list[str] = []
    if loaded:
        parts.append("env_files=" + ", ".join(loaded))
    else:
        parts.append(
            f"no_env_file (tried {project_root / '.env.local'}, {project_root / '.env'}, find_dotenv)"
        )
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    parts.append("OPENAI_API_KEY=" + ("set" if key else "missing"))
    if key:
        parts.append(f"key_len={len(key)}")
    return loaded, " | ".join(parts)
