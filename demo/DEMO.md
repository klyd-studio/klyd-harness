# Demo runbook

Two ways to run the klyd demo. Read the difference before filming.

| | model | honest use |
|---|---|---|
| **Live** | a real provider, your key | film this |
| **Rehearsal** | `demo/stub_server.py` | practice, or a fallback if the network dies — say so on screen |

Everything except the model's answer is identical in both. The git hook, the
diff, the prompt, the SQLite write, the ranking, and the injection file are the
shipping code path either way.

---

## Prerequisites

0.2.4 is on PyPI. Install from there so the demo proves the shipping artifact,
not a source checkout. Run the shell blocks in Git Bash on Windows: the git
hooks are POSIX `sh`, and PowerShell will not run them.

```sh
py -m venv demo-venv
source demo-venv/Scripts/activate       # Windows, in Git Bash
# or: python3 -m venv demo-venv && source demo-venv/bin/activate
pip install klyd==0.2.4
kl --version
```

The source tree is only needed for the rehearsal stub (`demo/stub_server.py`).
If you are hacking on klyd itself, use `pip install -e .` from the repo.

Optional, for the last shot: an agent CLI on `PATH` (`aider` or `opencode`).
Without one, end the demo at the injection file — that is the part klyd owns.

---

## Live demo (90 seconds, with a key)

```sh
# 1. a fresh repo, so nothing is pre-seeded
mkdir demo-repo && cd demo-repo
git init -q
git config user.email you@example.com
git config user.name You

# 2. install the harness
kl init
ls .git/hooks/post-commit      # show the absolute launcher, not a bare `kl`
cat .gitignore                 # .klyd/ is ignored: config.json holds the key

# 3. configure the provider once
kl config --openrouter-key sk-or-... --model openrouter/free
kl config --show               # key is masked
# openrouter/free is the demo default: it needs no credit and the extraction
# prompt does not depend on a specific model. If your key has credit, swap in
# the model you intend to film, e.g. anthropic/claude-sonnet-4-6.

# 4. a commit with an architectural decision in it
mkdir -p worker/src/lib
cat > worker/src/lib/jwt.ts <<'EOF'
export async function verify(token: string, key: CryptoKey) {
  const { header, payload, signature } = split(token);
  // the alg header is attacker-controlled; always use the configured algorithm
  return crypto.subtle.verify("HMAC", key, signature, payload);
}
EOF
git add . && git commit -m "feat(auth): verify JWTs with WebCrypto, ignore alg header"

# the post-commit hook runs extraction in the background. Wait until status
# shows the decision; if the provider is slow, run `kl extract-commit` in the
# foreground. Repeating extraction is safe: an identical decision reinforces.
sleep 5
kl status
tail -5 .klyd/errors.log       # should be empty of tracebacks
```

Then the two shots that matter:

```sh
kl status                      # the decision, module, confidence
# json.tool parses the stream, so a truncated or invalid document fails here.
kl status --format json | python -m json.tool | head

# stage a file the extracted decision governs. The old runbook staged a file
# it never created, so preview correctly showed an empty injection on camera.
cat > worker/src/lib/session.ts <<'EOF'
export const sessionTtlSeconds = 60;
EOF
git add worker/src/lib/session.ts
kl preview-injection           # the exact text the agent will see
kl prepare-injection           # writes .klyd/injection.txt
cat .klyd/injection.txt
```

If the model chose file patterns that do not match `worker/src/lib/session.ts`,
preview is empty. That is the file filter working. For the shot, pin the
decision and say why:

```sh
kl pin 1                       # use the id from `kl status`
kl preview-injection
```

If an agent is installed:

```sh
kl run aider                   # or: kl run opencode
```

## Rehearsal (no key)

```sh
# terminal 1: from the klyd source checkout
py demo/stub_server.py --port 8765

# terminal 2: from demo-repo
kl config --api-base http://127.0.0.1:8765/v1 --model gpt-4o-mini
kl extract-commit
kl status
```

The stub prints the prompt it received, including the `diff --git` lines, which
is useful to show that klyd is sending the real diff and not a summary.

If you film this, put one line on screen: *model response stubbed for the
recording*. The rest of the run is real.

---

## What to say while it runs

- The hook is `post-commit` and it is dumb: it calls `kl extract-commit` and
  exits. All logic is in the CLI.
- The model returns a decision, not code. What is stored is one sentence about
  the architecture, the module it belongs to, and the file patterns it governs.
- The next session gets the decisions that match the files being staged —
  not the whole memory store.
- `kl status --format json` is the contract a script or an editor can consume.

## Failure modes that are now handled (do not demo the old ones)

- `kl init` from a subdirectory initialises the repository root.
- `kl init` twice is safe.
- `kl init` ignores `.klyd/`, so `git add .` cannot stage the provider key.
- `kl --help` in an unrelated directory leaves nothing behind.
- `kl status --format json` survives decisions that carry embeddings.
- `kl extract-commit` exits 1 and prints the reason when there is no key or no
  commit yet.
- `kl review` reaches its prompt instead of crashing on a logging key.
- `kl preview-injection` prints the exact text written to `.klyd/injection.txt`,
  including `[module]` tags and line breaks.
- A pinned decision appears even when the staged files do not match its
  patterns.
- HTML and Markdown exports escape model-written text.
- `kl run` propagates the agent's exit code; `--no-inject` passes no memory;
  generic agents receive the injection as a final argument.
- `kl unpin 999` says the decision was not pinned instead of claiming success.
- Injection files are UTF-8 with LF endings, so non-ASCII decisions survive.

## What is proven, and what is not

**Proven on 2026-10-31 with a real free-tier OpenRouter key.** `openrouter/free`
routed to `cohere/north-mini-code:free`. The full loop ran on the network: commit
→ hook → provider → SQLite → `kl status` → valid JSON → `.klyd/injection.txt`.
The second commit was clean (`errors.log` 0 bytes). A deliberately contradicting
commit was flagged, and `kl review` option `a` produced a real LLM merge that was
accepted. The transcript is in the audit write-up (`05-cli-audit.md`).

**Also proven on 2026-11-06, against the published 0.2.3 wheel and the built
0.2.4 wheel:** every klyd command was exercised, including all six `kl review`
actions, every status export, pin/unpin, and `kl run` with a stub agent. The
0.2.4 suite is 35 tests. The defects listed above were found by that audit and
fixed in 0.2.4.

**Not proven:** the specific paid model `anthropic/claude-sonnet-4-6`. The test
key had $0 credit, so a paid call would 402. The wire path is the same; the model
name is the only difference. If your key has credit, run the live demo once with
the model you intend to film. `kl run` with a real `aider` or `opencode` binary is
also still unexercised; its argument wiring was tested with compiled stubs.
