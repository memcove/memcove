"""Structured audit log for guarded data operations.

Every accepted read/derive/export emits one JSON line on the ``memcove.audit``
logger: which tenant ran what (rewritten) SQL and how much it returned. Operators
route this logger wherever their audit sink lives. Auditing must never break a
request, so serialization failures are swallowed.
"""

from __future__ import annotations

import logging

import orjson

_log = logging.getLogger("memcove.audit")


def audit(event: str, **fields) -> None:
    """Emit a single structured audit record."""
    try:
        _log.info(
            orjson.dumps(
                {"event": event, **fields}, default=str, option=orjson.OPT_SORT_KEYS
            ).decode()
        )
    except Exception:  # noqa: BLE001 - auditing is best-effort, never fatal
        _log.info("audit event=%s (unserializable fields)", event)
