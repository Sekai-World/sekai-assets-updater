"""Log redaction for URLs, headers, cookies, and free-form process output."""

import re
from typing import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_HEADER_RE = re.compile(
    r"(?:^|[-_])(authorization|cookie|set-cookie|api[-_]?key|api[-_]?token|"
    r"access[-_]?token|refresh[-_]?token|auth[-_]?token|client[-_]?secret|"
    r"secret|credential)(?:$|[-_])",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|[-_])(signature|sig|token|access[-_]?token|auth|authorization|"
    r"api[-_]?key|secret|credential|policy|key|key[-_]?id|access[-_]?key|"
    r"aws[-_]?access[-_]?key[-_]?id|expires|date)(?:$|[-_])|"
    r"^(?:x[-_]amz|cloudfront)[-_]",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?<![\w-])"
    r"(?P<prefix>(?:token|api[-_]key|api[-_]token|access[-_]token|refresh[-_]token|"
    r"auth[-_]token|authorization|proxy[-_]authorization|client[-_]secret|"
    r"secret|credential|body|response[-_]body)\b\s*[:=]\s*)"
    r"(?:(?P<single>'(?P<single_value>[^']*)')|"
    r"(?P<double>\"(?P<double_value>[^\"]*)\")|"
    r"(?P<bare>[^\s,;]+))",
    re.IGNORECASE,
)
_TOKEN_HEADER_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\bX[-_](?:Api[-_]Token|Access[-_]Token)\b\s*[:=]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_API_KEY_HEADER_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\bX[-_]Api[-_]Key\b\s*[:=]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:authorization|proxy[-_]authorization)\b\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_COOKIE_MORSEL_RE = re.compile(
    r"(?P<name>[^\s=;,]+)(?P<separator>\s*=\s*)"
    r"(?:(?P<single>'[^']*')|(?P<double>\"[^\"]*\")|(?P<bare>[^;\s,]+))",
)
_COOKIE_TEXT_RE = re.compile(
    r"(?P<prefix>\bCookie\b\s*[:=]\s*)(?P<value>[^\r\n]*)",
    re.IGNORECASE,
)
_REDACTED = "<redacted>"


def sanitize_headers(headers: Mapping | None) -> dict[str, str]:
    """Return headers safe to include in logs.

    Header names are treated case-insensitively.  In particular, this avoids
    accidentally exposing credentials when a caller uses a differently cased
    spelling of ``Cookie`` or an API key header.
    """
    if not headers:
        return {}
    sanitized = {}
    for name, value in headers.items():
        name_text = str(name)
        if name_text.casefold() == "cookie":
            sanitized[name_text] = _sanitize_cookie_value(str(value))
        else:
            sanitized[name_text] = (
                _REDACTED if _SENSITIVE_HEADER_RE.search(name_text) else str(value)
            )
    return sanitized


def _sanitize_cookie_value(value: str) -> str:
    sanitized = _COOKIE_MORSEL_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}{_REDACTED}",
        value,
    )
    return sanitized if sanitized != value else _REDACTED


def _sanitize_assignment(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{_REDACTED}"


def sanitize_url(url: str) -> str:
    """Redact signed and credential-bearing query values without hiding the URL."""
    try:
        parts = urlsplit(str(url))
        query = urlencode(
            [
                (key, _REDACTED if _SENSITIVE_QUERY_RE.search(key) else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        # Logging must not turn an otherwise useful error into a second error.
        return "<invalid-url>"


def sanitize_http_log_value(value):
    """Sanitize common HTTP values before they are passed to a logger."""
    if isinstance(value, Mapping):
        return sanitize_headers(value)
    if isinstance(value, str) and "://" in value:
        value = _HTTP_URL_RE.sub(lambda match: sanitize_url(match.group(0)), value)
    if isinstance(value, str):
        value = _COOKIE_TEXT_RE.sub(
            lambda match: f"{match.group('prefix')}{_sanitize_cookie_value(match.group('value'))}",
            value,
        )
        value = _API_KEY_HEADER_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
        value = _TOKEN_HEADER_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
        value = _AUTHORIZATION_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
        value = _SENSITIVE_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
    return value


def sanitize_log_label(value) -> str:
    """Return a safe, printable label for pipeline diagnostics."""
    return str(sanitize_http_log_value(str(value)))
