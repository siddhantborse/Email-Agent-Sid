"""
Step 4 -- LOG.

One JSON object per line, appended. This file is the point of the whole
read-only phase: you read it, you correct it, and those corrections become both
your calibration data and your eval set.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .schemas import Decision

_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = _ROOT / "logs"
LOG_PATH = LOG_DIR / "decisions.jsonl"

# A committed run from a known-good, fully-throttled eval. The dashboard falls
# back to this when no fresh log exists, so a demo never depends on a live API
# key, a network, or a free-tier quota that resets once a day.
GOLDEN_PATH = _ROOT / "data" / "golden_run.jsonl"


def write(decisions: list[Decision], path: Path | None = None) -> Path:
    """Append decisions to the log. Creates the directory on first run."""
    path = path or LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        for d in decisions:
            row = d.model_dump()
            row["logged_at"] = stamp
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return path


def read(path: Path | None = None, *, golden: bool = False, all_runs: bool = False) -> list[dict]:
    """
    Read the most recent run back.

    The file is append-only -- it is an audit trail, and losing the history of
    what the agent decided would be the wrong trade. But every consumer of this
    function wants *a run*, not the concatenation of every run ever made, and
    reading it as one blob is silently wrong the moment the corpus changes size.

    It was: a 35-email run and a 49-email run left 84 rows with 35 duplicated
    ids, and `dashboard.py` computed accuracy, lane distribution and the
    confusion matrix across the union of both -- a Gemini run averaged with a
    llama3.2 run. Rows are grouped by `logged_at`, which `write` stamps once per
    call, and only the newest group is returned. `all_runs=True` for the whole
    history.

    With no fresh log (or `golden=True`), falls back to the committed run in
    data/golden_run.jsonl so the dashboard always has something real to show.
    Returns [] only if neither exists.
    """
    if golden:
        path = GOLDEN_PATH
    else:
        path = path or LOG_PATH
        if not path.exists() and GOLDEN_PATH.exists():
            path = GOLDEN_PATH

    if not path.exists():
        return []

    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    if all_runs or not rows:
        return rows

    # Rows without a stamp are older than this field and sort last, so a log
    # written before `logged_at` existed still yields something rather than
    # nothing.
    newest = max(r.get("logged_at", "") for r in rows)
    return [r for r in rows if r.get("logged_at", "") == newest]
