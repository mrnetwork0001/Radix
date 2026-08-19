"""Where ingestion keeps its disk caches.

The repo checkout and the container image nest this package at different
depths (``backend/app/ingest`` vs ``/app/app/ingest``), so a fixed
``parents[N]`` that resolves to the project root in one lands on the
filesystem root in the other - which is how the sentinel ended up trying to
create an unwritable ``/data`` and crash-looping. Probe for the directory that
actually exists instead, and let a deployment override it outright.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("radix.ingest.paths")


def data_dir() -> Path:
    """The project's ``data/`` directory, in either layout."""
    override = os.getenv("RADIX_DATA_DIR")
    if override:
        return Path(override).expanduser()

    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "data"
        if candidate.is_dir():
            return candidate
    return Path(tempfile.gettempdir()) / "radix-data"


def cache_dir(name: str) -> Path:
    """A ready-to-use cache directory, or a temp fallback if it is not writable.

    A cache is never worth crashing a long-running process over: the sentinel
    must keep sweeping advisories even when its disk cache cannot be created.
    """
    target = data_dir() / "cache" / name
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except OSError as error:
        fallback = Path(tempfile.gettempdir()) / "radix-cache" / name
        fallback.mkdir(parents=True, exist_ok=True)
        log.warning("cache dir %s unusable (%s); falling back to %s", target, error, fallback)
        return fallback
