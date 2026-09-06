"""
Lookalike sender detection. Deterministic, no model.

An email from `d.okafor@knowncompany-support.example` claiming to be a colleague
is a spoof of `knowncompany.example`. Asking a model to be suspicious of that
works until it doesn't -- the model reasons about tone and content, and a good
impersonation has innocent tone and plausible content.

Domain similarity is a string problem, so we solve it as one. This runs in the
safety layer on sender metadata only, never on the body, and its result can only
raise a lane.

Configure the domains you actually deal with via SID_KNOWN_DOMAINS in .env:

    SID_KNOWN_DOMAINS=knowncompany.example,myemployer.com
"""

import os


def domain_of(address: str) -> str:
    """Extract a lowercase domain from an email address."""
    return address.strip().lower().rpartition("@")[2]


def known_domains() -> set[str]:
    raw = os.getenv("SID_KNOWN_DOMAINS", "")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def _core(domain: str) -> str:
    """The registrable-ish part: 'mail.knowncompany.example' -> 'knowncompany'."""
    parts = [p for p in domain.split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein, early-exit once it exceeds `cap`. Small strings only."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def lookalike_of(sender_email: str, known: set[str] | None = None) -> str | None:
    """
    The known domain this sender is imitating, or None.

    An exact match is not a lookalike -- it is the real thing. A subdomain of a
    known domain is also fine. What we catch is close-but-different:

        knowncompany-support.example   vs  knowncompany.example   -> caught
        knowncompanny.example          vs  knowncompany.example   -> caught
        mail.knowncompany.example      vs  knowncompany.example   -> fine
        totallyunrelated.example       vs  knowncompany.example   -> not a lookalike
                                                                    (just unknown)
    """
    known = known_domains() if known is None else known
    if not known:
        return None

    domain = domain_of(sender_email)
    if not domain or domain in known:
        return None

    for good in known:
        # A real subdomain of a domain we trust.
        if domain.endswith("." + good):
            return None

    core = _core(domain)
    if not core:
        return None

    for good in known:
        good_core = _core(good)
        if not good_core or core == good_core:
            continue
        # One core wraps the other: "knowncompany-support" around "knowncompany".
        if good_core in core or core in good_core:
            return good
        # Or it is a near-miss typo: "knowncompanny".
        if _edit_distance(core, good_core) <= 2:
            return good

    return None
