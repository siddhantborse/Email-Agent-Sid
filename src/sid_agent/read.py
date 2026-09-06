"""
Step 1 -- READ.

Masks sensitive values *before* the AI ever sees the email. This is not a
classifier and it does not use a model: it is regex, it runs first, and it
cannot be talked out of running.

The point: the agent's OTP safety does not depend on the AI deciding to be
careful. It depends on the code never being in the text the AI reads.
"""

import re
import unicodedata

# Characters that are invisible when rendered but break a regex that expects
# letters to be adjacent. "verifica\u200btion code" reads identically to a human
# and matches nothing at all, which is a free bypass of the entire masking layer.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\u2061-\u2064\ufeff\u00ad]")

# A run of 4-12 digits that tolerates the separators people actually use when
# reading a code aloud or when a mail client wraps it: "839214", "839 214",
# "8-3-9-2-1-4". Written once and shared, because every keyword pattern below
# needs the same tolerance.
_DIGITS = r"\d(?:[\s.\-]?\d){3,11}"

# Words that introduce a one-time secret. Bare "code" is deliberately NOT in
# here -- "bug 12345 in the code" would mask, and since masked content forces an
# ESCALATE, over-masking costs accuracy rather than safety. It is admitted below
# only in the shapes that are unambiguous: "your code", "code:", "code is".
_OTP_WORDS = (
    r"(?:otp|one[\s-]?time(?:\s+(?:code|password|pin|passcode))?|verification\s+code|"
    r"security\s+code|access\s+code|auth(?:entication)?\s+code|passcode|"
    r"confirmation\s+code|login\s+code|sign[\s-]?in\s+code|recovery\s+code|"
    r"2fa|mfa|two[\s-]?factor|pin(?:\s+(?:code|number))?|"
    r"(?:your|the)\s+code\b|code\s*(?::|is\b))"
)

# Each entry: (label, compiled pattern). The label is what we record; the value
# is replaced and never stored anywhere.
#
# Every window below uses [\s\S] rather than [^\n]. A code on its own line under
# its label -- "Your code:\n839214" -- is the single most common shape a real
# provider sends, and a newline-blind window missed all of them.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # "your verification code is 123456", "OTP: 4821", "code 55213 expires"
    (
        "otp",
        re.compile(
            rf"(?i)\b{_OTP_WORDS}[\s\S]{{0,40}}?({_DIGITS})"
        ),
    ),
    # the reverse order: "123456 is your verification code"
    (
        "otp",
        re.compile(
            rf"(?i)({_DIGITS})[\s\S]{{0,40}}?\b(?:is\s+your\s+)?{_OTP_WORDS}"
        ),
    ),
    ("bank_account", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b")),  # IBAN
    # payment card numbers, with or without spaces/dashes/dots, wrapped or not
    (
        "card_number",
        re.compile(r"\b\d(?:[ .\-]?\d){11,17}\b"),
    ),
    (
        "card_number",
        re.compile(
            r"(?i)\b(?:card|acct|account)\s*(?:number|no\.?|#)?\s*[:\-]?\s*"
            r"[\s\S]{0,10}?\b\d(?:[ .\-]?\d){11,17}\b"
        ),
    ),
    # "password: hunter2", "pwd is ...", "credentials - ..."
    (
        "password",
        re.compile(
            r"(?i)\b(?:password|passwd|pass\s?phrase|pwd|credentials?)\b"
            r"\s*(?:is|are|:|=|-)\s*(\S{4,})"
        ),
    ),
    # bearer tokens / long API-key-shaped strings
    (
        "token",
        re.compile(r"\b(?:sk-|pk-|ghp_|gho_|github_pat_|xox[baprs]-|Bearer\s+)[A-Za-z0-9_\-]{16,}"),
    ),
    # provider-specific key shapes worth naming, because they are unmistakable
    ("token", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),                      # AWS access key
    ("token", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),                # Google API key
    (
        "token",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    ),                                                                    # JWT
    (
        "token",
        re.compile(
            r"(?i)\b(?:api[\s_\-]?key|secret|access[\s_\-]?token|auth[\s_\-]?token)\b"
            r"\s*(?:is|:|=)\s*[\'\"]?([A-Za-z0-9_\-]{16,})"
        ),
    ),
    # government identifiers. Not secrets exactly, but never something an agent
    # should be reasoning about in a prompt.
    ("national_id", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),             # US SSN
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
    # Normalise before anything looks at the text. NFKC folds fullwidth and
    # other compatibility digits to ASCII, and the invisible-character strip
    # closes the "verifica<zero-width-space>tion code" bypass. Both run here
    # rather than inside mask() so that the text the model reads and the text
    # the patterns matched against are the same string.
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
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
