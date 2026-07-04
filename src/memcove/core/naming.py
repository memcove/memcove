"""Validation for object labels."""

from __future__ import annotations

import re

from memcove.core.errors import MemcoveError

_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def validate_label(label: str) -> str:
    """Ensure a label is a safe, lowercase Iceberg table name."""
    candidate = (label or "").strip().lower()
    if not _LABEL_RE.match(candidate):
        raise MemcoveError(
            f"invalid label {label!r}: use lowercase letters, digits and underscores, "
            "starting with a letter (max 128 chars)"
        )
    return candidate
