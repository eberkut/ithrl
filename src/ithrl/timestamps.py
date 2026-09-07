"""Validate timestamps shared by captured evidence formats."""

from __future__ import annotations

import re
from datetime import datetime

_RFC3339_TIMESTAMP = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_EXPECTED_FORMAT = "YYYY-MM-DDTHH:MM:SS[.fraction](Z|+HH:MM|-HH:MM)"


def validate_rfc3339_timestamp(value: str, *, field: str) -> str:
    """Return a timestamp after validating the supported RFC 3339 grammar."""

    if _RFC3339_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{field} must use RFC 3339 format {_EXPECTED_FORMAT}")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{field} must be a valid RFC 3339 timestamp") from error
    return value
