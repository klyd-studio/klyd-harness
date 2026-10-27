#!/usr/bin/env python3
"""Retrieval quality eval for klyd's file-pattern matcher.

`get_decisions_for_files` is the function every injection depends on: given the
paths you are about to touch, it returns the decisions that apply. It ranks by
`reinforcement_count * confidence weight`, filtered by a glob match. That is a
heuristic, not a semantic search, and until it is measured it is a guess.

Two suites, because a single suite lies:

  exact       the path is the one the decision was written against. This is the
              easy case, and it is what a demo shows you.
  realistic   files were renamed, moved, or are new modules with no precedent.
              This is what a term of real commits actually looks like.

Reporting only the first suite would be dishonest, and reporting only the second
would overstate how broken the system is. The gap between them is the finding:
glob matching has no notion of file identity, so a rename silently loses the
decision.

Output is ASCII-only so it runs in a Windows cp1252 console as well as a POSIX
one.

Run from the repository root:

    py demo/eval_retrieval.py

No API key, no network, no model calls. This measures the retrieval layer in
isolation, which is the part that decides whether the model ever sees the right
memory.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from klyd.db import get_decisions_for_files, init_db, store_decision  # noqa: E402

# --- corpus -----------------------------------------------------------------
# Modelled on the Codify codebase: the kind of decisions `kl extract-commit`
# would have stored over a term of real commits.

DECISIONS = [
    dict(
        decision="JWT signatures are checked with WebCrypto; the alg header is ignored and must never be trusted.",
        module="auth",
        file_patterns="worker/src/lib/jwt.ts,worker/src/middleware/auth.ts",
        confidence="HIGH",
    ),
    dict(
        decision="Scoring is one three-statement D1 batch gated on changes() = 1; do not split it into separate requests.",
        module="scoring",
        file_patterns="worker/src/handlers/adminReview.ts,worker/src/handlers/submissions.ts",
        confidence="HIGH",
    ),
    dict(
        decision="Rankings are read from xp_awards, never from a cached total on users.",
        module="scoring",
        file_patterns="worker/src/handlers/leaderboard.ts",
        confidence="HIGH",
    ),
    dict(
        decision="Student code runs in Pyodide in the browser; the worker never executes a submission.",
        module="execution",
        file_patterns="frontend/src/lib/pyodide.ts,frontend/src/pages/InterpreterPage.tsx",
        confidence="HIGH",
    ),
    dict(
        decision="Admin routes must be registered with requireAdmin, never checked inside the handler.",
        module="auth",
        file_patterns="worker/src/router.ts,worker/src/middleware/admin.ts",
        confidence="HIGH",
    ),
    dict(
        decision="Competition state changes go through competition_integrity triggers; do not write status directly.",
        module="competition",
        file_patterns="worker/src/lib/competition.ts,database/migrations/*",
        confidence="MEDIUM",
    ),
    dict(
        decision="The frontend talks to the worker through api.ts only, so the base URL stays in one place.",
        module="frontend",
        file_patterns="frontend/src/lib/api.ts",
        confidence="MEDIUM",
    ),
    dict(
        decision="Grade eligibility is enforced server-side; the client hides problems it must not show.",
        module="competition",
        file_patterns="worker/src/handlers/publicCompetition.ts,frontend/src/pages/CompetitionLobbyPage.tsx",
        confidence="MEDIUM",
    ),
    dict(
        decision="Every response uses the {status, data} envelope so the frontend has one error path.",
        module="api",
        file_patterns="worker/src/handlers/*",
        confidence="LOW",
    ),
    dict(
        decision="Theme values come from ThemeProvider; no hardcoded colours in components.",
        module="frontend",
        file_patterns="frontend/src/components/*,frontend/src/pages/*",
        confidence="LOW",
    ),
]

# --- cases ------------------------------------------------------------------
# expected = the decisions a reviewer would agree are relevant to this change.

CASES = [
    # exact: the path the decision was recorded against
    dict(suite="exact", name="touching JWT verification",
         files=["worker/src/lib/jwt.ts"],
         expected=["JWT signatures"]),
    dict(suite="exact", name="reviewing a submission",
         files=["worker/src/handlers/adminReview.ts"],
         expected=["Scoring is one three-statement", "Every response uses"]),
    dict(suite="exact", name="changing the leaderboard",
         files=["worker/src/handlers/leaderboard.ts"],
         expected=["Rankings are read from xp_awards", "Every response uses"]),
    dict(suite="exact", name="editing the interpreter UI",
         files=["frontend/src/pages/InterpreterPage.tsx"],
         expected=["Student code runs in Pyodide", "Theme values come from"]),
    dict(suite="exact", name="adding an admin route",
         files=["worker/src/router.ts", "worker/src/middleware/admin.ts"],
         expected=["Admin routes must be registered"]),
    dict(suite="exact", name="writing a migration",
         files=["database/migrations/0011_new.sql"],
         expected=["Competition state changes"]),
    dict(suite="exact", name="changing the API client",
         files=["frontend/src/lib/api.ts"],
         expected=["The frontend talks to the worker"]),
    dict(suite="exact", name="unrelated file (should be empty)",
         files=["README.md"],
         expected=[]),

    # realistic: renamed, moved, or new
    dict(suite="realistic", name="pyodide moved to runtime/",
         files=["frontend/src/runtime/pyodide.ts"],
         expected=["Student code runs in Pyodide"]),
    dict(suite="realistic", name="scoring extracted to services/",
         files=["worker/src/services/scoring.ts"],
         expected=["Scoring is one three-statement"]),
    dict(suite="realistic", name="jwt renamed to tokens.ts",
         files=["worker/src/lib/tokens.ts"],
         expected=["JWT signatures"]),
    dict(suite="realistic", name="new handler file",
         files=["worker/src/handlers/attendance.ts"],
         expected=["Every response uses"]),
    dict(suite="realistic", name="new page dir",
         files=["frontend/src/features/editor/Editor.tsx"],
         expected=["Theme values come from"]),
    dict(suite="realistic", name="migration in a subdir",
         files=["database/migrations/2026/0012.sql"],
         expected=["Competition state changes"]),
]

K = 3


def matches(decision_text: str, expected_prefix: str) -> bool:
    return decision_text.startswith(expected_prefix)


def build_db() -> str:
    path = str(Path(tempfile.mkdtemp()) / "memory.db")
    init_db(path)
    for decision in DECISIONS:
        store_decision(path, {**decision, "event_type": "NEW"})
    return path


def run_suite(db: str, suite: str) -> dict:
    cases = [c for c in CASES if c["suite"] == suite]
    hits = expected_total = returned_total = relevant_total = 0
    reciprocal_ranks: list[float] = []
    misses: list[str] = []

    print(f"\n-- {suite} ({len(cases)} cases)\n")

    for case in cases:
        returned = [r["decision"] for r in get_decisions_for_files(db, case["files"], top_k=K)]
        relevant = [t for t in returned if any(matches(t, e) for e in case["expected"])]
        found = {e for e in case["expected"] if any(matches(t, e) for t in returned)}

        hits += len(found)
        expected_total += len(case["expected"])
        returned_total += len(returned)
        relevant_total += len(relevant)

        if case["expected"]:
            rank = next(
                (i + 1 for i, t in enumerate(returned) if any(matches(t, e) for e in case["expected"])),
                None,
            )
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)

        ok = len(found) == len(case["expected"])
        print(f"  [{'ok  ' if ok else 'MISS'}] {case['name']:<34} returned {len(returned)}")
        for text in returned:
            tag = "relevant" if any(matches(text, e) for e in case["expected"]) else "noise"
            print(f"          - ({tag}) {text[:70]}")
        if not ok:
            missing = [e for e in case["expected"] if e not in found]
            misses.append(f"{case['name']}: missing {missing}")

    return {
        "precision": relevant_total / returned_total if returned_total else 0.0,
        "recall": hits / expected_total if expected_total else 0.0,
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "retrieved": returned_total,
        "relevant": relevant_total,
        "hits": hits,
        "expected": expected_total,
        "misses": misses,
    }


def main() -> int:
    db = build_db()
    print(f"corpus: {len(DECISIONS)} decisions, {len(CASES)} cases, k={K}")

    results = {suite: run_suite(db, suite) for suite in ("exact", "realistic")}

    print("\n-- summary\n")
    print(f"  {'suite':<12}{'precision@3':>13}{'recall@3':>11}{'MRR':>8}")
    for suite, r in results.items():
        print(f"  {suite:<12}{r['precision']:>13.2f}{r['recall']:>11.2f}{r['mrr']:>8.2f}")

    print("\n  reading: precision and recall fall off when a file is renamed or a")
    print("  module is new, because the matcher compares paths as strings. A")
    print("  decision attached to frontend/src/lib/pyodide.ts is invisible once")
    print("  that file is frontend/src/runtime/pyodide.ts, even though it is the")
    print("  same decision about the same code.")

    for suite, r in results.items():
        if r["misses"]:
            print(f"\n  failures ({suite}):")
            for miss in r["misses"]:
                print(f"    - {miss}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
