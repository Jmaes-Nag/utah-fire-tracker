"""Small helpers shared by every module."""

import math
import time


class UpstreamError(Exception):
    def __init__(self, detail="Upstream service returned invalid data.", status_code=502, partial=None):
        super().__init__(detail)
        self.status_code = status_code
        self.partial = partial


def finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def utc_stamp(timestamp=None):
    """ISO-8601 UTC timestamp for an epoch time (default: now)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
