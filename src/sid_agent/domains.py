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
import re


def domain_of(address: str) -> str:
    """Extract a lowercase domain from an email address."""
    return address.strip().lower().rpartition("@")[2]


def known_domains() -> set[str]:
    raw = os.getenv("SID_KNOWN_DOMAINS", "")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


# Below this length a core is too short for substring matching to mean anything:
# "n" is a substring of "knowncompany" and flagging it as impersonation is noise.
MIN_CORE_FOR_SUBSTRING = 4


def _core(domain: str) -> str:
    """The registrable-ish part: 'mail.knowncompany.example' -> 'knowncompany'."""
    parts = [p for p in domain.split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _labels(domain: str) -> list[str]:
    """Every dot-separated label. 'knowncompany.example.evil.com' -> [.., 'evil', 'com']."""
    return [p for p in domain.split(".") if p]


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

    # A trailing dot is the DNS root and `knowncompany.example.` resolves to the
    # same host as `knowncompany.example` -- strip it before any comparison, or
    # a single character defeats the entire check.
    domain = domain_of(sender_email).rstrip(".")
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
        if not good_core:
            continue

        # Same name, different suffix: knowncompany.com against
        # knowncompany.example. We reach here only after the exact-match and
        # subdomain checks above have already returned, so equal cores at this
        # point mean the registrable name was reused under a suffix we do not
        # trust. This is the single most common real spoof shape and it used to
        # be skipped outright by `if core == good_core: continue`.
        if core == good_core:
            return good

        # The trusted name pushed down into a label of someone else's domain:
        # knowncompany.example.evil.com, or knowncompany.secure-mail.example.
        # The core is 'evil' / 'secure-mail', so a core-only comparison never
        # sees it -- but the name is right there in the address, which is the
        # whole point of the trick.
        if good_core in _labels(domain)[:-2] or good_core in _labels(domain)[:-1]:
            return good

        # The trusted name wrapped in something longer: "knowncompany-support".
        # Only this direction. The reverse -- a short core that happens to be a
        # substring of the trusted name -- flags "know.example" as impersonating
        # "knowncompany.example", which is simply a different company. That
        # false positive fails safe, but a noisy signal is one people learn to
        # ignore, and this one has to survive being believed.
        if len(good_core) >= MIN_CORE_FOR_SUBSTRING and good_core in core:
            return good

        # Or it is a near-miss typo: "knowncompanny", "knowwncommpanny". The cap
        # scales with the name -- two edits in a six-letter domain is a different
        # domain, three edits in a twelve-letter one is someone fat-fingering it.
        if _edit_distance(core, good_core, cap=6) <= max(2, len(good_core) // 4):
            return good

    return None

# Characters people put between words in a display name, and the ones an
# impersonator puts there to break a naive substring match.
_NAME_NOISE = re.compile(r"[^a-z0-9]+")


def _squash(text: str) -> str:
    """Lowercase and strip everything that is not alphanumeric."""
    return _NAME_NOISE.sub("", text.lower())


def display_name_spoof(
    sender: str, sender_email: str, known: set[str] | None = None
) -> str | None:
    """
    The known domain this sender's *name* claims to be, while its address is not.

    `lookalike_of` reads the domain and nothing else, which leaves the oldest
    trick in the book undefended:

        Priya Raman (KnownCompany) <priya.knowncompany@gmail.com>

    The domain is `gmail.com` -- genuinely unrelated, not a lookalike of
    anything, so the domain check correctly says nothing. But the part a human
    actually reads says KnownCompany, and in most mail clients the address is
    hidden behind that name entirely.

    So: if the trusted name appears in the display name or the local part, and
    the address is not actually at that domain, the sender is claiming an
    affiliation the address does not support. Metadata only, never the body.

    Returns the domain being claimed, or None.
    """
    known = known_domains() if known is None else known
    if not known:
        return None

    domain = domain_of(sender_email).rstrip(".")
    local = sender_email.strip().lower().rpartition("@")[0]
    claim = _squash(sender) + " " + _squash(local)

    for good in known:
        good_core = _squash(_core(good))
        # Short cores are substrings of too many ordinary names to be evidence.
        if len(good_core) < MIN_CORE_FOR_SUBSTRING:
            continue
        if good_core not in _squash(claim):
            continue
        # The name checks out if the address is actually at that domain.
        if domain == good or domain.endswith("." + good):
            continue
        return good

    return None
