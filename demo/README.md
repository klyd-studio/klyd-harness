# Demo assets

The canonical runbook is [DEMO.md](DEMO.md). It covers the live loop (needs a
provider key) and the labelled stub rehearsal (no key, says so on screen).

`seed.py` is a separate offline path. It fills `.klyd/memory.db` with sample
decisions so you can film status, retrieval, and review without a model call.
The data is seeded, not extracted; label it that way if it appears on screen.

## Install for any path

0.2.3 is not on PyPI yet, and 0.2.2 is broken. Install from the audit branch:

```sh
git clone -b fix/clean-install-audit https://github.com/klyd-studio/klyd-harness
cd klyd-harness
pip install .
```

## Seeded path (no key, ~40 seconds)

```sh
mkdir -p /tmp/klyd-demo && cd /tmp/klyd-demo && git init -q
kl init
rm -f .klyd/memory.db
python /path/to/klyd-harness/demo/seed.py

kl status                   # decisions, modules, confidence, state
kl status --tree            # grouped by module
kl metrics                  # counts, average confidence, conflict rate
```

Then the part worth filming — retrieval by the files you are about to touch:

```sh
mkdir -p frontend/src/pages
echo '// wip' > frontend/src/pages/CompetitionPage.tsx
git add frontend/src/pages/CompetitionPage.tsx
kl preview-injection        # matches the pyodide decision for this path
kl prepare-injection        # writes .klyd/injection.txt
cat .klyd/injection.txt
```

And conflict review:

```sh
kl status                   # two CONTRADICT rows are flagged
kl review                   # accept / merge / edit / reject, interactively
```

**What to say over it:** the store is a local SQLite file; retrieval is a
single query that glob-matches the staged paths and ranks by file match and
recency; nothing leaves the repository.

## Hook notes

`kl init` bakes the absolute launcher into `.git/hooks/post-commit`, so `kl`
does not need to be on `PATH` for the hook to run. The hook sends stdout to
`/dev/null` and only stderr to `.klyd/errors.log`; a clean commit leaves that
file empty.

`kl init` also appends `.klyd/` to `.gitignore`, because `.klyd/config.json`
stores the provider key in plaintext. Without that rule the demo's `git add .`
would commit the key.

The hook runs extraction in the background. For a live demo, either wait a few
seconds after the commit or run `kl extract-commit` in the foreground in a
repository where you have removed the hook. Do not run both at once unless you
want to show deduplication.

On Windows, use Git Bash. The hooks are POSIX `sh`; `cmd` and PowerShell will
not run them.
