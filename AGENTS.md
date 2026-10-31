# AGENTS.md

## What this project is
klyd: a CLI tool that wraps coding agents via git hooks to inject architectural memory.

## Stack (frozen — do not deviate)
- Python 3.11+
- Click for CLI
- SQLite via stdlib sqlite3 (no ORM)
- POSIX shell for hook scripts
- Anthropic API (claude-sonnet-4-6) via BYOK

## Module responsibilities (do not blur these)
- cli.py: Click entrypoint only. No business logic.
- db.py: All SQLite operations. Nothing else.
- extractor.py: One function — LLM extraction call. Nothing else.
- injector.py: One function — format injection string. Nothing else.
- hooks.py: Install/uninstall git hooks. Nothing else.

## Hard rules
- Two LLM calls per commit cycle maximum
- Every function does one thing
- Shell hooks are dumb — they call klyd CLI, contain no logic themselves
- Never add a dependency to solve a problem that stdlib solves

## Dev commands
- Install locally: `pip install .` or `pip install -e .`
- CLI entry: `kl` (defined in pyproject.toml)
- Test file: `test_db.py` (no formal test suite yet)

## Database
- Schema lives at `klyd/schema/v1.sql`
- db.py uses raw sqlite3, no ORM

## Testing
- `pytest -q` runs the unit tests plus `test_cli_e2e.py`, which drives the
  installed CLI in throwaway git repositories (init, hooks, status, review,
  exports). No network or API key required.
- Manual extraction test: `test_task09/` (2 commits: sqlite3 then click).
  Checkout each commit and run `kl extract-commit`, then check `kl status`.
- Model: `openrouter/free` works; `anthropic/*` models need OpenRouter key

## Known issues (fixed but worth knowing)
- First commit: use `git show HEAD` not `git diff HEAD~1 HEAD`
- OpenRouter free tier has limited credits; monitor 402 errors
- Model names with `/` (e.g., `anthropic/claude-3.5-sonnet`) route through OpenRouter

## Remote
- GitHub: https://github.com/klyd-studio/klyd-harness
