# Retrieval quality

`get_decisions_for_files` is the function every injection depends on. It ranks by
`reinforcement_count * confidence weight` over glob-matched file patterns. That is
a heuristic, and until it was measured it was a guess.

This is the measurement. Run it yourself:

```sh
py demo/eval_retrieval.py
```

## Scope

The corpus is hand-written to model the Codify codebase; it is not telemetry from
a term of real commits. Treat the numbers as a demonstration of the failure mode
(the matcher has no notion of file identity), not as a production metric. The
`--stdin` and file-path behaviour of the CLI itself is covered by
`test_cli_e2e.py`, which runs the installed binary.

Ten decisions, fourteen cases, k=3. Two suites:

- **exact** — the path the decision was written against.
- **realistic** — files renamed, moved, or new modules with no precedent.

## Result

| suite | precision@3 | recall@3 | MRR |
|---|---|---|---|
| exact | 1.00 | 1.00 | 1.00 |
| realistic | 1.00 | **0.33** | 0.33 |

## Reading it

Precision holds at 1.00 because the matcher returns *nothing* rather than
something wrong. It is never incorrect.

Recall falls to 0.33 the moment a path changes, because a decision is attached to
a glob and a glob has no notion of file identity. A decision written against
`frontend/src/lib/pyodide.ts` is invisible once that file is
`frontend/src/runtime/pyodide.ts` — same decision, same code, no match.

So the retrieval layer is precisely useless: **never wrong, and silent when it
matters most.** Four of six realistic cases returned an empty set:

- `pyodide moved to runtime/`
- `scoring extracted to services/`
- `jwt renamed to tokens.ts`
- `new page dir`

The two that survived did so incidentally — `worker/src/handlers/*` is broad
enough to catch a new handler file, and `database/migrations/*` matches a
subdirectory because SQLite's `GLOB`/`fnmatch` treatment of `*` happens to span
`/` here.

## What this implies

Ranked by expected value:

1. **Track renames.** A path→decision index keyed on file identity (git's rename
   detection gives this for free on `post-commit`) would recover most of the
   missing recall.
2. **Fall back to embeddings on a miss.** `compute_embedding` currently returns a
   dummy vector and the column is unused. An empty result is the one case where a
   semantic fallback is cheap and clearly correct.
3. **Do not widen the globs.** That trades the one property that works —
   precision — for recall, and pushes noise into the injection file.

## Why this file exists

Because "it seems to work" is not a measurement. The first version of this eval
scored 1.00 on everything, which meant it was testing the easy case only. Adding
renames produced the 0.33. The honest number is the one that came after making
the test harder.
