# Demo script

Two ways to show klyd. Path A needs no API key and takes about 40 seconds of
video. Path B is the real loop and needs one.

Record in a terminal with a large font, and keep the prompt visible — half the
story is that this runs inside a normal repository, not a hosted service.

---

## Path A — the memory, without a model call (no key)

```sh
# a throwaway repo so nothing touches a real project
mkdir -p /tmp/klyd-demo && cd /tmp/klyd-demo && git init -q

# install klyd into the demo, then seed a small decision history
pip install klyd            # or: pip install -e ~/path/to/klyd-harness
kl init
rm -f .klyd/memory.db && python ~/path/to/klyd-harness/demo/seed.py

kl status                   # table: decision / module / confidence / state
kl status --tree            # grouped by module
kl metrics                  # counts, average confidence, conflict rate
```

Then the part worth filming — retrieval by the files you are about to touch:

```sh
mkdir -p frontend/src/pages && echo '// wip' > frontend/src/pages/CompetitionPage.tsx
git add frontend/src/pages/CompetitionPage.tsx
kl preview-injection        # matches the pyodide decision for this path
kl prepare-injection        # writes .klyd/injection.txt for the agent
cat .klyd/injection.txt
```

And conflict review, which is the actual reason this exists:

```sh
kl status                   # two CONTRADICT rows are flagged
kl review                   # accept / merge / edit / reject, interactively
```

**What to say over it:** the store is a local SQLite file; retrieval is a
single query that glob-matches the staged paths and ranks by similarity and
recency; nothing leaves the repository.

---

## Path B — the real loop (needs a key)

```sh
mkdir -p /tmp/klyd-live && cd /tmp/klyd-live && git init -q
kl init
kl config --api-key sk-ant-...        # or --openai-key / --openrouter-key

cat > db.py <<'PY'
import sqlite3
def connect(): return sqlite3.connect("app.db")
PY
git add db.py && git commit -qm "feat(db): use one SQLite connection helper instead of global instances"

kl extract-commit                     # foreground, so the model call is visible
kl status
```

Then the loop closes:

```sh
cat >> db.py <<'PY'
# added a cache layer so hot reads skip the helper
PY
git add db.py && git commit -qm "perf(db): cache hot reads in front of the connection helper"
kl extract-commit && kl status        # NEW / REINFORCE / CONTRADICT
```

**What to say over it:** the commit is the input, the diff is classified, and
only decisions are stored — not code, not the whole message.

---

## Gotchas while filming

- **The `post-commit` hook runs extraction in the background.** It is
  `kl extract-commit >> .klyd/errors.log 2>&1 &`, so a commit returns
  immediately and gives you nothing to watch. For the video, run
  `kl extract-commit` in the foreground, and mention that the hook does the
  same thing unattended.
- **`kl` must be on `PATH`** for the hooks to work. If you install with pipx or
  into a venv, activate it first, or the hook silently writes an error to
  `.klyd/errors.log`.
- **On Windows, use Git Bash.** The hooks are POSIX `sh`; `cmd` and PowerShell
  will not run them.
- **Check `.klyd/errors.log` after the first commit** — it is the only place
  failures appear.
- **Do not film the provider key.** Run `kl config --show` only if the key is
  masked; otherwise skip it.
