"""
Step 1 -- READ.

Masks sensitive values *before* the AI ever sees the email. This is not a
classifier and it does not use a model: it is regex, it runs first, and it
cannot be talked out of running.

The point: the agent's OTP safety does not depend on the AI deciding to be
careful. It depends on the code never being in the text the AI reads.
"""

import re

# Each entry: (label, compiled pattern). The label is what we record; the value
# is replaced and never stored anywhere.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # "your verification code is 123456", "OTP: 4821", "code 55213 expires"
    (
        "otp",
        re.compile(
            r"(?i)\b(?:otp|one[\s-]?time(?:\s+(?:code|password|pin))?|verification\s+code|"
            r"security\s+code|access\s+code|auth(?:entication)?\s+code|passcode|"
            r"confirmation\s+code|2fa|two[\s-]?factor)\b[^\n]{0,40}?\b(\d{4,8})\b"
        ),
    ),
    # the reverse order: "123456 is your verification code"
    (
        "otp",
        re.compile(
            r"(?i)\b(\d{4,8})\b[^\n]{0,40}?\b(?:is\s+your\s+)?(?:otp|verification\s+code|"
            r"security\s+code|one[\s-]?time\s+(?:code|password|pin)|passcode)\b"
        ),
    ),
    # payment card numbers, with or without spaces/dashes
    (
        "card_number",
        re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
    ),
    # "password: hunter2", "pwd is ..."
    (
        "password",
        re.compile(r"(?i)\b(?:password|passwd|pwd)\b\s*(?:is|:|=)\s*(\S{4,})"),
    ),
    # bearer tokens / long API-key-shaped strings
    (
        "token",
        re.compile(r"\b(?:sk-|pk-|ghp_|xox[baprs]-|Bearer\s+)[A-Za-z0-9_\-]{16,}\b"),
    ),
]

_REDACTED = "[{label} removed before the agent read this]"


def mask(text: str) -> tuple[str, list[str]]:
    """
    Replace sensitive values in `text`.

    Returns the masked text and a sorted list of the *kinds* of thing removed
    (e.g. ["otp"]). The removed values themselves are never returned, logged,
    or stored.
    """
    if not text:
        return "", []

    found: set[str] = set()
    masked = text

    for label, pattern in _PATTERNS:
        def _sub(match: re.Match, _label: str = label) -> str:
            found.add(_label)
            return _REDACTED.format(label=_label)

        masked = pattern.sub(_sub, masked)

    return masked, sorted(found)


def clean(text: str, limit: int = 4000) -> str:
    """Collapse whitespace and truncate. Keeps prompts small and predictable."""
    if not text:
        return ""
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + "\n... [truncated]"
    return text


def prepare(subject: str, body: str) -> tuple[str, str, list[str]]:
    """Mask and tidy a subject/body pair. Returns (subject, body, masked_labels)."""
    masked_subject, s_found = mask(clean(subject, limit=300))
    masked_body, b_found = mask(clean(body))
    return masked_subject, masked_body, sorted(set(s_found) | set(b_found))
