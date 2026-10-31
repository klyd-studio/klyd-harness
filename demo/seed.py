#!/usr/bin/env python3
"""Seed .klyd/memory.db with sample decisions for demos and screenshots.

This is demo data, not extracted data. It exists so the read paths — status,
--tree, metrics, preview-injection — can be shown without spending model calls
or waiting on a provider. The write path (a real commit being extracted) is
what `kl extract-commit` is for.

Usage, inside the repository you want to demo:

    python demo/seed.py
    kl status
    kl status --tree
    kl metrics

Delete .klyd/memory.db first if you want to reseed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from klyd.db import flag_decision, init_db, store_decision

# A small, plausible decision history for a project like Codify. Kept short on
# purpose: the demo is about retrieval and conflict review, not volume.
DECISIONS = [
    {
        "decision": "JWT signatures are verified with WebCrypto and the alg header is ignored; never trust it.",
        "module": "auth",
        "file_patterns": "worker/src/lib/jwt.ts,worker/src/middleware/*",
        "confidence": "HIGH",
        "event_type": "NEW",
    },
    {
        "decision": "Scoring is one three-statement D1 batch gated on changes() = 1; do not split it into separate requests.",
        "module": "scoring",
        "file_patterns": "worker/src/handlers/*,database/migrations/*",
        "confidence": "HIGH",
        "event_type": "NEW",
    },
    {
        "decision": "Student code runs in Pyodide inside the browser; the backend never executes a submission.",
        "module": "execution",
        "file_patterns": "frontend/src/lib/pyodide.ts,frontend/src/pages/*",
        "confidence": "HIGH",
        "event_type": "NEW",
    },
    {
        "decision": "At most one competition may be unreset; a partial unique index enforces it.",
        "module": "db",
        "file_patterns": "database/migrations/*",
        "confidence": "MEDIUM",
        "event_type": "NEW",
    },
    {
        "decision": "Rankings are read from xp_awards, never from a cached total on users.",
        "module": "scoring",
        "file_patterns": "worker/src/handlers/leaderboard.ts",
        "confidence": "HIGH",
        "event_type": "NEW",
    },
    {
        "decision": "Use Clerk for authentication instead of the custom WebCrypto flow.",
        "module": "auth",
        "file_patterns": "worker/src/lib/*,frontend/src/lib/*",
        "confidence": "MEDIUM",
        "event_type": "CONTRADICT",
    },
    {
        "decision": "Execute submissions in a server-side container so hidden tests can run.",
        "module": "execution",
        "file_patterns": "worker/src/handlers/*",
        "confidence": "LOW",
        "event_type": "CONTRADICT",
    },
]


def main() -> int:
    db_dir = Path(".klyd")
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / "memory.db"

    if db_path.exists():
        print(f"{db_path} already exists — remove it first to reseed.", file=sys.stderr)
        return 1

    init_db(str(db_path))

    stored: list[tuple[int, dict]] = []
    for decision in DECISIONS:
        stored.append((store_decision(str(db_path), decision), decision))

    # Conflicts are what `kl review` acts on, so flag the contradictory ones.
    for decision_id, decision in stored:
        if decision["event_type"] == "CONTRADICT":
            flag_decision(str(db_path), decision_id)

    print(f"Seeded {len(stored)} decisions into {db_path}")
    print(f"  {sum(1 for _, d in stored if d['event_type'] == 'CONTRADICT')} flagged for review")
    print()
    print("Next: kl status        (table of decisions)")
    print("      kl status --tree (grouped by module)")
    print("      kl metrics       (counts and conflict rate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
