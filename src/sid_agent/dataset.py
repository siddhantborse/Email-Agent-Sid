"""
Loads the labeled email corpus from data/.

Each entry carries an `expected_lane` -- the lane a careful human would pick.
That label is what turns this from a demo into something measurable.

The corpus is split across several files rather than one, because the
adversarial set grows on a different schedule from the ordinary mail and it is
useful to be able to read the attacks on their own. Any `data/*.json` file with
an `emails` key is picked up automatically, so adding a new batch of attack
cases means dropping in a file -- not editing a loader.
"""

import json
from pathlib import Path

from .read import prepare
from .schemas import Email

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_PATH = DATA_DIR / "emails.json"


def corpus_files(directory: Path | None = None) -> list[Path]:
    """
    Every corpus file in data/, base set first.

    Sorted so the ids stay in a stable order across runs -- an eval whose row
    order changes between runs is needlessly hard to diff.
    """
    directory = directory or DATA_DIR
    base = directory / "emails.json"
    others = sorted(
        f for f in directory.glob("*.json")
        if f != base and "emails" in json.loads(f.read_text(encoding="utf-8"))
    )
    return ([base] if base.exists() else []) + others


class Case:
    """One labeled email: the masked Email, plus the ground truth about it."""

    def __init__(self, email: Email, expected_lane: str, note: str, tags: list[str]):
        self.email = email
        self.expected_lane = expected_lane
        self.note = note
        self.tags = tags

    @property
    def adversarial(self) -> bool:
        return "adversarial" in self.tags


def load(path: Path | None = None) -> list[Case]:
    """
    Read the corpus and mask every email exactly as a real one would be.

    Pass `path` to score a single file; the default reads every corpus file in
    data/. Duplicate ids across files are a mistake worth failing on rather than
    silently letting one entry shadow another.
    """
    paths = [path] if path else corpus_files()

    entries: list[dict] = []
    seen: dict[str, Path] = {}
    for file in paths:
        for entry in json.loads(file.read_text(encoding="utf-8"))["emails"]:
            if entry["id"] in seen:
                raise ValueError(
                    f"duplicate email id {entry['id']!r} in {file.name} "
                    f"(already defined in {seen[entry['id']].name})"
                )
            seen[entry["id"]] = file
            entries.append(entry)

    cases: list[Case] = []
    for entry in entries:
        subject, body, masked = prepare(entry["subject"], entry["body"])
        cases.append(
            Case(
                email=Email(
                    id=entry["id"],
                    thread_id=entry["id"],
                    sender=entry.get("sender", ""),
                    sender_email=entry.get("sender_email", ""),
                    subject=subject,
                    body=body,
                    date="(corpus)",
                    masked=masked,
                ),
                expected_lane=entry["expected_lane"],
                note=entry.get("note", ""),
                tags=entry.get("tags", []),
            )
        )
    return cases


def emails(path: Path | None = None) -> list[Email]:
    """Just the emails, without their labels."""
    return [c.email for c in load(path)]
