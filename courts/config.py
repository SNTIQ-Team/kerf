"""Paths and crawler identity."""
from __future__ import annotations

import os
from pathlib import Path

# A descriptive, contact-bearing identity without personal data.
USER_AGENT = (
    "SNTIQ-Legal-Courts/0.1 (+https://sntiq.com; local legal research corpus; "
    "<=1 req/s)"
)
# Never faster than one request per second per host.
MIN_INTERVAL_SECONDS = 1.1
DB_FILENAME = "courts.sqlite"


def data_home() -> Path:
    """Corpus root; override with SNTIQ_COURTS_HOME (tests use a temp dir)."""
    configured = os.environ.get("SNTIQ_COURTS_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "sntiq-legal" / "courts"


def db_path(home: Path | None = None) -> Path:
    return (home or data_home()) / DB_FILENAME


def raw_dir(home: Path | None = None) -> Path:
    return (home or data_home()) / "raw"
