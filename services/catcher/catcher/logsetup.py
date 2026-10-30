"""Logging with a redaction filter so secrets never reach the log stream."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

REDACTED = "[REDACTED]"

# Token shapes that must never be logged even if we don't know the exact value
# (GitHub installation/app tokens, Anthropic OAuth tokens, bearer headers, PEM keys).
_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)(bearer|token)\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
]


def redact(text: str, secrets: Iterable[str]) -> str:
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            msg = str(record.msg)
        record.msg = redact(msg, self.secrets)
        record.args = None
        if record.exc_info:
            # Render the traceback now, redacted, and drop the raw exception.
            text = logging.Formatter().formatException(record.exc_info)
            record.msg = f"{record.msg}\n{redact(text, self.secrets)}"
            record.exc_info = None
            record.exc_text = None
        return True


def setup_logging(secrets: Iterable[str], level: str = "INFO") -> RedactingFilter:
    flt = RedactingFilter(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(flt)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # aiohttp access logs include headers only at debug; keep them quiet.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return flt
