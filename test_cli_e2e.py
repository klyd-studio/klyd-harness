"""End-to-end tests for the `kl` command line.

These are deliberately black-box: every test spawns the CLI the way a user
would, in a throwaway git repository, and asserts on exit codes and output.
They exist because the unit tests all passed while the product was broken --
`kl init` installed hooks that called a bare `kl` that was not on PATH, so
automatic extraction silently did nothing; `prepare-injection` imported three
functions that did not exist, so the injection path raised before printing;
`kl review` crashed on a reserved logging key. None of that is visible without
running the binary.

Run with:

    pytest -q test_cli_e2e.py

No network and no API key are required. The one test that exercises the
extraction path asserts the *failure* message, because extraction needs a
provider key by design.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(repo: Path, *args: str, input_text: str | None = None):
    """Run `python -m klyd.cli ...` inside *repo* and return the process."""
    return subprocess.run(
        [sys.executable, "-m", "klyd.cli", *args],
        cwd=str(repo),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q").returncode == 0
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    return repo


def init_repo(repo: Path) -> None:
    proc = run(repo, "init")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (repo / ".klyd" / "memory.db").is_file()


def add_decision(repo: Path, decision: str, module: str = "auth", **overrides):
    """Insert a decision through the real storage layer."""
    from klyd.db import store_decision

    payload = {
        "decision": decision,
        "module": module,
        "file_patterns": f"{module}/*",
        "confidence": "HIGH",
        "event_type": "NEW",
        "last_seen_commit": "abc1234",
        **overrides,
    }
    return store_decision(str(repo / ".klyd" / "memory.db"), payload)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_is_idempotent_and_preserves_data(tmp_path):
    """Running `kl init` again must not destroy the memory store.

    It used to fail with `table decisions already exists`, which is exactly
    what a user hits after pulling a repo that already has `.klyd/`.
    """
    repo = make_repo(tmp_path)
    init_repo(repo)
    add_decision(repo, "JWT signatures are verified with WebCrypto.")

    second = run(repo, "init")
    assert second.returncode == 0, second.stdout + second.stderr

    status = run(repo, "status", "--format", "json")
    assert status.returncode == 0
    payload = json.loads(status.stdout)
    assert [d["decision"] for d in payload["active_decisions"]] == [
        "JWT signatures are verified with WebCrypto."
    ]


def test_init_from_subdirectory_initialises_repository_root(tmp_path):
    """`kl init` in a nested directory must target the git root, not the cwd."""
    repo = make_repo(tmp_path)
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)

    proc = run(nested, "init")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (repo / ".klyd" / "memory.db").is_file()
    assert not (nested / ".klyd").exists()


def test_hooks_are_installed_with_a_usable_launcher(tmp_path):
    """Hooks must not depend on `kl` being on the git process's PATH.

    Git runs hooks with the PATH of the client that invoked it, so a virtualenv
    install produced `kl: command not found` and extraction never ran.
    """
    repo = make_repo(tmp_path)
    init_repo(repo)

    for hook_name, command in (
        ("pre-commit", "prepare-injection"),
        ("post-commit", "extract-commit"),
    ):
        hook = repo / ".git" / "hooks" / hook_name
        assert hook.is_file(), f"{hook_name} was not installed"
        content = hook.read_text(encoding="utf-8")
        assert command in content
        # CRLF breaks `#!/bin/sh` under Git for Windows.
        assert "\r" not in content
        # The old template's first command was the bare name `kl`.
        body = [line for line in content.splitlines() if command in line][0]
        assert not body.lstrip().startswith("kl "), body
        assert "-m klyd.cli" in body or os.path.isabs(body.split()[0].strip("'\"")), body


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_works_from_a_subdirectory(tmp_path):
    """Every command used to resolve `.klyd/` against the cwd only."""
    repo = make_repo(tmp_path)
    init_repo(repo)
    add_decision(repo, "Rankings are read from xp_awards.")

    nested = repo / "worker" / "src"
    nested.mkdir(parents=True)
    proc = run(nested, "status")
    assert proc.returncode == 0
    assert "Rankings are read from xp_awards." in proc.stdout
    assert "not initialized" not in proc.stdout


def test_help_outside_a_project_does_not_create_a_stray_klyd_dir(tmp_path):
    """Importing the logger used to mkdir `.klyd` in whatever directory you ran from."""
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = run(plain, "--help")
    assert proc.returncode == 0
    assert not (plain / ".klyd").exists()


def test_status_json_is_machine_readable(tmp_path):
    """`--format json` must not be word-wrapped by the terminal renderer.

    Long decision strings used to acquire literal newlines at 80 columns, which
    made `json.loads` fail on the tool's own output.
    """
    repo = make_repo(tmp_path)
    init_repo(repo)
    long_text = (
        "Scoring is one three-statement D1 batch gated on changes() = 1; do not "
        "split it into separate requests, because the gate is what makes awards "
        "idempotent under concurrent reviewers and resubmissions."
    )
    add_decision(repo, long_text)

    proc = run(repo, "status", "--format", "json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["active_decisions"][0]["decision"] == long_text

    markdown = run(repo, "status", "--format", "markdown")
    assert markdown.returncode == 0
    assert markdown.stdout.startswith("# klyd Status")
    assert not markdown.stdout.lstrip().startswith("\u250c")  # no rendered table


def test_status_empty_state_is_readable(tmp_path):
    """The empty state must be a panel, not a six-column table with one cell."""
    repo = make_repo(tmp_path)
    init_repo(repo)
    proc = run(repo, "status")
    assert proc.returncode == 0
    assert "No active architectural decisions stored yet." in proc.stdout
    # A wrapped sentence inside a table used to contain line-per-word breaks.
    assert "architect\u00ad" not in proc.stdout


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_show_renders_list_values(tmp_path):
    """`config --show` crashed with NotRenderableError on the default config."""
    repo = make_repo(tmp_path)
    init_repo(repo)
    proc = run(repo, "config", "--show")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "pinned_decision_ids" in proc.stdout
    assert "NotRenderableError" not in proc.stderr


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


def test_review_reaches_the_prompt_without_crashing(tmp_path):
    """`kl review` died with KeyError: 'module' before printing a conflict."""
    from klyd.db import flag_decision

    repo = make_repo(tmp_path)
    init_repo(repo)
    add_decision(repo, "JWT signatures are checked with WebCrypto.", module="auth")
    conflict_id = add_decision(
        repo, "Use Clerk for authentication instead.", module="auth"
    )
    flag_decision(str(repo / ".klyd" / "memory.db"), conflict_id)

    proc = run(repo, "review", input_text="s\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Conflict 1 of 1" in proc.stdout
    assert "Select action" in proc.stdout
    assert "Traceback" not in proc.stderr
    assert "KeyError" not in proc.stderr


# ---------------------------------------------------------------------------
# pin / unpin
# ---------------------------------------------------------------------------


def test_pin_unknown_decision_exits_non_zero(tmp_path):
    """A scripting caller must be able to tell success from failure."""
    repo = make_repo(tmp_path)
    init_repo(repo)
    proc = run(repo, "pin", "999")
    assert proc.returncode == 1
    assert "not found" in proc.stdout


# ---------------------------------------------------------------------------
# agent command construction
# ---------------------------------------------------------------------------


def test_opencode_gets_a_prompt_not_a_model_name():
    """`-m` in OpenCode selects the model, so the old argv was silently wrong."""
    from klyd.cli import _agent_command

    injection_path = Path(".klyd") / "injection.txt"
    argv = _agent_command(["opencode"], injection_path, "INJECTION")

    assert argv == ["opencode", "run", "INJECTION"]
    assert "-m" not in argv

    # Do not add a second `run` when the user already supplied one.
    argv = _agent_command(["opencode", "run", "--model", "x"], injection_path, "INJ")
    assert argv == ["opencode", "run", "--model", "x", "INJ"]


def test_aider_gets_a_message_file():
    from klyd.cli import _agent_command

    injection_path = Path(".klyd") / "injection.txt"
    argv = _agent_command(["aider"], injection_path, "INJECTION")

    assert argv == ["aider", "--message-file", str(injection_path)]


# ---------------------------------------------------------------------------
# extraction failure path
# ---------------------------------------------------------------------------


def test_extract_commit_without_a_key_explains_itself(tmp_path):
    """The core loop needs an API key; its absence must be actionable."""
    repo = make_repo(tmp_path)
    (repo / "app.ts").write_text("export const xp = 0;\n", encoding="utf-8")
    git(repo, "add", "app.ts")
    assert git(repo, "commit", "-qm", "initial").returncode == 0

    # Install after the commit so the post-commit hook is not involved.
    init_repo(repo)
    proc = run(repo, "extract-commit")

    assert proc.returncode == 1
    combined = proc.stdout + proc.stderr
    assert "No valid API key" in combined
    assert "kl config --api-key" in combined
    assert "Traceback" not in proc.stderr
