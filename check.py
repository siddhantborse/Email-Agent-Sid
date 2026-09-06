#!/usr/bin/env python
"""
Run everything that can be checked without an API key.

  python check.py

Five harnesses, no network, no model:

  verify.py            the safety properties, swept exhaustively
  mutation_test.py     breaks each rule to prove those checks can fail
  calibration_eval.py  does it actually ask less over time
  situations_eval.py   the proactive path
  worst_case.py        what the code still catches with the model gone

This exists because "you can verify the safety claims without trusting me with
a key" is only true if it is also *easy*. Exit code is non-zero if any harness
fails, so it works as a pre-commit hook or a CI step unchanged.

The one thing it cannot do is score the model's judgement -- lane accuracy needs
`python eval.py` and a key. Everything here is about what the code guarantees
regardless of what the model says.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent

CHECKS = [
    ("safety properties", ["verify.py"]),
    ("can those checks fail?", ["mutation_test.py"]),
    ("calibration", ["calibration_eval.py"]),
    ("proactive path", ["situations_eval.py"]),
    ("compromised model", ["worst_case.py"]),
]


def main() -> int:
    results = []
    for name, argv in CHECKS:
        print(f"\n{'=' * 70}\n  {name}  --  python {' '.join(argv)}\n{'=' * 70}")
        code = subprocess.run(
            [sys.executable, *[str(ROOT / a) if a.endswith(".py") else a for a in argv]],
            cwd=ROOT,
        ).returncode
        results.append((name, code))

    print(f"\n{'=' * 70}")
    width = max(len(n) for n, _ in results)
    for name, code in results:
        print(f"  {name.ljust(width)}   {'PASS' if code == 0 else f'FAIL ({code})'}")

    failed = [n for n, c in results if c != 0]
    if failed:
        print(f"\n  {len(failed)} failed: {', '.join(failed)}\n")
        return 1
    print("\n  All offline checks passed. No API key was used.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
