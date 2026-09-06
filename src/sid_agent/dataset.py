"""
Loads the labeled email corpus in data/emails.json.

Each entry carries an `expected_lane` -- the lane a careful human would pick.
That label is what turns this from a demo into something measurable.
"""

import json
from pathlib import Path

from .read import prepare
from .schemas import Email

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "emails.json"


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
    """Read the corpus and mask every email exactly as a real one would be."""
    path = path or DATA_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))

    cases: list[Case] = []
    for entry in raw["emails"]:
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
