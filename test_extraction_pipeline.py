"""End-to-end test of the extraction loop against a local model server.

The only part of klyd that a unit test cannot reach is the model call: the
provider URLs are hardcoded, and a real key costs money. `api_base` exists so
the OpenAI-compatible client can be pointed at any gateway -- a self-hosted
proxy, LiteLLM, Ollama, or this test's stub.

So this test drives the *real* CLI, over a real HTTP connection, and asserts
that a commit becomes a stored decision and then an injection file. The model's
answer is canned; everything around it is the shipping code path.

Run with:

    pytest -q test_extraction_pipeline.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from test_cli_e2e import git, init_repo, make_repo, run


# ---------------------------------------------------------------------------
# A minimal OpenAI-compatible server
# ---------------------------------------------------------------------------


class StubHandler(BaseHTTPRequestHandler):
    # Class-level state so each test can set the reply and inspect requests.
    reply_text: str = "[]"
    status_code: int = 200
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw),
            }
        )

        if type(self).status_code != 200:
            self.send_response(type(self).status_code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = json.dumps(
            {"choices": [{"message": {"content": type(self).reply_text}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the default stderr access log
        pass


@pytest.fixture
def stub_server():
    StubHandler.reply_text = "[]"
    StubHandler.status_code = 200
    StubHandler.requests = []

    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def silent_server():
    """Accept connections and never answer, like a stalled provider."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    held: list[socket.socket] = []

    def accept_and_hold():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            held.append(conn)

    thread = threading.Thread(target=accept_and_hold, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        stop.set()
        for conn in held:
            conn.close()
        listener.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


DECISION = (
    "JWT signatures are verified with WebCrypto and the alg header is ignored; "
    "never trust it."
)


def reply_with(*decisions: dict) -> None:
    StubHandler.reply_text = json.dumps(list(decisions))


def commit_auth_change(repo) -> None:
    (repo / "worker").mkdir(exist_ok=True)
    (repo / "worker" / "auth.ts").write_text(
        "export function verify(token: string) {\n"
        "  return crypto.subtle.verify(token);\n"
        "}\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    assert git(repo, "commit", "-qm", "feat(auth): verify JWTs with WebCrypto").returncode == 0


def configure_stub(repo, base_url: str):
    proc = run(repo, "config", "--api-base", base_url, "--model", "gpt-4o-mini")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def test_commit_becomes_a_decision_and_then_an_injection(tmp_path, stub_server):
    """The whole product, once: diff -> model -> SQLite -> injection file."""
    server, base_url = stub_server
    reply_with(
        {
            "decision": DECISION,
            "module": "auth",
            "file_patterns": "worker/*",
            "confidence": "HIGH",
            "event": "NEW",
            "semantic_summary": "JWTs are verified with WebCrypto.",
        }
    )

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, base_url)

    extract = run(repo, "extract-commit")
    assert extract.returncode == 0, extract.stdout + extract.stderr

    # 1. The request that reached the server looks like an OpenAI call.
    request = StubHandler.requests[-1]
    assert request["path"] == "/v1/chat/completions"
    assert request["authorization"] == "Bearer not-needed"
    assert request["body"]["model"] == "gpt-4o-mini"
    prompt = request["body"]["messages"][0]["content"]
    assert "feat(auth): verify JWTs with WebCrypto" in prompt
    assert "crypto.subtle.verify" in prompt

    # 2. The decision is in the store, machine-readable.
    status = run(repo, "status", "--format", "json")
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert [d["decision"] for d in payload["active_decisions"]] == [DECISION]

    # 3. A staged file matching the decision's pattern pulls it into injection.
    (repo / "worker" / "session.ts").write_text(
        "export const ttl = 60;\n", encoding="utf-8"
    )
    git(repo, "add", "worker/session.ts")

    prepare = run(repo, "prepare-injection")
    assert prepare.returncode == 0, prepare.stdout + prepare.stderr

    injection = (repo / ".klyd" / "injection.txt").read_text(encoding="utf-8")
    assert DECISION in injection
    assert injection.startswith("[klyd] Architectural decisions")


def test_contradiction_is_stored_and_flagged(tmp_path, stub_server):
    """CONTRADICT must land in the store flagged, not silently accepted."""
    server, base_url = stub_server
    reply_with(
        {
            "decision": "Use Clerk for authentication instead of the custom WebCrypto flow.",
            "module": "auth",
            "file_patterns": "worker/*",
            "confidence": "MEDIUM",
            "event": "CONTRADICT",
            "resolution_suggestion": "Archive the WebCrypto decision.",
        }
    )

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, base_url)

    extract = run(repo, "extract-commit")
    assert extract.returncode == 0, extract.stdout + extract.stderr

    payload = json.loads(run(repo, "status", "--format", "json").stdout)
    assert payload["stats"]["flagged"] == 1
    flagged = payload["flagged_decisions"][0]
    assert "Clerk" in flagged["decision"]
    assert "[Resolution: Archive the WebCrypto decision.]" in flagged["decision"]


def test_model_returning_garbage_fails_loudly(tmp_path, stub_server):
    """A model that ignores the JSON contract must not look like success."""
    server, base_url = stub_server
    StubHandler.reply_text = "Sure! Here are the decisions: none."

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, base_url)

    proc = run(repo, "extract-commit")

    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "Extraction failed" in combined
    assert "Traceback" not in proc.stderr
    # The store must be untouched.
    payload = json.loads(run(repo, "status", "--format", "json").stdout)
    assert payload["active_decisions"] == []


def test_provider_error_fails_loudly(tmp_path, stub_server):
    """A 500 from the gateway is a failure, not an empty extraction."""
    server, base_url = stub_server
    StubHandler.status_code = 500

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, base_url)

    proc = run(repo, "extract-commit")

    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "Extraction failed" in combined
    assert "500" in combined


def test_stalled_provider_times_out_instead_of_hanging(tmp_path, silent_server):
    """A provider that accepts and then goes silent must not hang the hook.

    `urlopen` has no timeout by default. A live OpenRouter call left two
    post-commit processes blocked for minutes with an empty errors.log. The
    timeout must be bounded and the failure must name the cause.
    """
    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, silent_server)

    env = os.environ.copy()
    env["KLYD_HTTP_TIMEOUT"] = "0.2"
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "klyd.cli", "extract-commit"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "did not respond within 0.2s" in combined
    assert "KLYD_HTTP_TIMEOUT" in combined
    assert elapsed < 15, f"extraction took {elapsed:.1f}s; timeout did not fire"
    errors = (repo / ".klyd" / "errors.log").read_text(encoding="utf-8")
    assert "TimeoutError" in errors


# ---------------------------------------------------------------------------
# Regressions found by running the product, not the unit tests
# ---------------------------------------------------------------------------


def test_two_commits_in_a_row(tmp_path, stub_server):
    """The second extraction used to crash before it called the model.

    Decisions written by extraction carry an embedding BLOB. `json.dumps` on
    the existing-decision list therefore raised `TypeError: Object of type
    bytes is not JSON serializable` -- but only on the second commit, because
    the store is empty on the first. klyd worked exactly once per repository.
    """
    server, base_url = stub_server
    reply_with(
        {
            "decision": DECISION,
            "module": "auth",
            "file_patterns": "worker/*",
            "confidence": "HIGH",
            "event": "NEW",
        }
    )

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    # The hook would race the explicit call below; this test is about the CLI.
    (repo / ".git" / "hooks" / "post-commit").unlink()
    configure_stub(repo, base_url)

    first = run(repo, "extract-commit")
    assert first.returncode == 0, first.stdout + first.stderr

    reply_with(
        {
            "decision": "Session TTL is fixed at 60 seconds in one constant.",
            "module": "auth",
            "file_patterns": "worker/*",
            "confidence": "MEDIUM",
            "event": "NEW",
        }
    )
    (repo / "worker" / "session.ts").write_text(
        "export const ttl = 60;\n", encoding="utf-8"
    )
    git(repo, "add", ".")
    assert git(repo, "commit", "-qm", "feat(auth): add session ttl").returncode == 0

    second = run(repo, "extract-commit")
    assert second.returncode == 0, second.stdout + second.stderr

    # The first decision was serialised into the second prompt, so the model
    # could reinforce or contradict it. That serialisation is what crashed.
    second_prompt = StubHandler.requests[-1]["body"]["messages"][0]["content"]
    assert DECISION in second_prompt

    payload = json.loads(run(repo, "status", "--format", "json").stdout)
    assert len(payload["active_decisions"]) == 2


def test_redirected_output_survives_cp1252(tmp_path, stub_server):
    """The hook redirects stdout to a file; the spinner used to crash there.

    Windows picks cp1252 for a redirected stream, and Rich's spinner glyphs
    are not representable in it. Every clean commit wrote a traceback into
    `.klyd/errors.log`. The CLI now reconfigures stdout/stderr to UTF-8.
    """
    repo = make_repo(tmp_path)
    init_repo(repo)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    proc = subprocess.run(
        [sys.executable, "-m", "klyd.cli", "status"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UnicodeEncodeError" not in proc.stderr
    assert "charmap" not in proc.stderr


# ---------------------------------------------------------------------------
# Regressions found by exercising every command against the published wheel
# ---------------------------------------------------------------------------


def test_extraction_records_the_commit_hash(tmp_path, stub_server):
    """A new decision must carry the commit it came from.

    The INSERT path dropped `last_seen_commit`; only a duplicate update wrote
    it. `kl status --format json` and `kl review` therefore showed null or
    "unknown commit" for every freshly extracted decision.
    """
    server, base_url = stub_server
    reply_with(
        {
            "decision": DECISION,
            "module": "auth",
            "file_patterns": "worker/*",
            "confidence": "HIGH",
            "event": "NEW",
        }
    )

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, base_url)

    extract = run(repo, "extract-commit")
    assert extract.returncode == 0, extract.stdout + extract.stderr

    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    payload = json.loads(run(repo, "status", "--format", "json").stdout)
    assert payload["active_decisions"][0]["last_seen_commit"] == head


def test_model_entry_without_a_decision_is_skipped(tmp_path, stub_server):
    """A malformed `{}` entry must not become a stored "Unknown" decision."""
    server, base_url = stub_server
    reply_with({})

    repo = make_repo(tmp_path)
    commit_auth_change(repo)
    init_repo(repo)
    configure_stub(repo, base_url)

    extract = run(repo, "extract-commit")
    assert extract.returncode == 0, extract.stdout + extract.stderr

    payload = json.loads(run(repo, "status", "--format", "json").stdout)
    assert payload["active_decisions"] == []


def test_review_merge_uses_the_configured_api_base(tmp_path, stub_server):
    """Rehearsal with --api-base must cover the merge path too."""
    from klyd.db import flag_decision
    from test_cli_e2e import add_decision

    server, base_url = stub_server
    repo = make_repo(tmp_path)
    init_repo(repo)
    configure_stub(repo, base_url)
    add_decision(repo, "Auth uses WebCrypto.", module="auth")
    conflict_id = add_decision(repo, "Auth uses Clerk instead.", module="auth")
    flag_decision(str(repo / ".klyd" / "memory.db"), conflict_id)

    StubHandler.reply_text = "Auth uses WebCrypto with a strict algorithm allowlist."

    review = run(repo, "review", input_text="a\ny\n")
    assert review.returncode == 0, review.stdout + review.stderr
    assert "Merged decision created" in review.stdout

    payload = json.loads(run(repo, "status", "--format", "json").stdout)
    assert payload["stats"]["flagged"] == 0
    assert "strict algorithm allowlist" in payload["active_decisions"][0]["decision"]
    assert StubHandler.requests[-1]["path"] == "/v1/chat/completions"
