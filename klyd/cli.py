import click
import subprocess
import json
import os
import sys
import traceback
import urllib.request
from contextlib import contextmanager
from .hooks import install_hooks
from .config import init_config, set_config, get_config, get_all_config, get_injection_template, set_injection_template, get_strict_mode, set_strict_mode, get_pinned_decision_ids, add_pinned_decision_id, remove_pinned_decision_id, clear_pinned_decision_ids, get_max_decisions_inject, set_max_decisions_inject, get_min_confidence, set_min_confidence, get_module_filter, set_module_filter
from .db import get_schema_path, migrate_db, get_decision_by_id, create_decision_version, merge_decisions, auto_archive_old_decisions, update_metrics, get_metrics_summary
from .logger import setup_logger
import sqlite3
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.columns import Columns
from rich.prompt import Prompt
from rich.text import Text
from rich.tree import Tree
from rich.markdown import Markdown
import rich.box
import io
import datetime

logger = setup_logger(__name__)


def _force_utf8_streams():
    """Make redirected output survive on Windows.

    The git hooks redirect klyd's output to `.klyd/errors.log`. On Windows the
    default encoding for a redirected stream is cp1252, so the spinner glyphs
    raised UnicodeEncodeError and the hook wrote a traceback to a file named
    "errors" on every clean commit. Reconfiguring only applies to real streams;
    pytest's capture objects do not have reconfigure().
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass


_force_utf8_streams()
console = Console()


@contextmanager
def _status(message):
    """Show a spinner only when a human can see it.

    Rich's Live renderer writes control sequences and spinner glyphs even when
    stdout is redirected, which is how the git hook ended up writing a
    traceback into `.klyd/errors.log`. When the output is not a terminal there
    is nobody to show a spinner to, so skip it entirely.
    """
    if console.is_terminal:
        with console.status(message, spinner="dots12"):
            yield
    else:
        yield


class KlydGroup(click.Group):
    def format_help(self, ctx, formatter):
        console.print()
        super().format_help(ctx, formatter)


def _find_klyd_root(start=None):
    """Return the nearest ancestor (or cwd) containing an initialised .klyd.

    The marker is `memory.db`, not the directory: importing the logger used to
    create a stray `.klyd/` in whatever directory you happened to be in, so an
    empty directory is not evidence of a project.
    """
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / '.klyd' / 'memory.db').exists():
            return candidate
    return None


def _find_git_root(start=None):
    """Return the git worktree root, or None if this is not a repository."""
    try:
        out = subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=str(start or Path.cwd()),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(out) if out else None


@click.group(cls=KlydGroup)
@click.pass_context
def cli(ctx):
    """klyd: a CLI tool that wraps coding agents via git hooks to inject architectural memory."""
    # Every command resolves `.klyd/...` relative to the working directory. A
    # developer is usually in a subdirectory, not the repo root, so enter the
    # project root first. Without this, `kl status` reported "not initialized"
    # from any nested directory and `prepare-injection` silently did nothing.
    root = _find_klyd_root()
    if root is not None and Path.cwd().resolve() != root:
        os.chdir(root)

def echo_brand(msg, bold=False):
    console.print(f"[cyan bold]klyd[/cyan bold] | {msg}")

# ----------------------------------------------------------------------
# LLM helper for merge suggestions
# ----------------------------------------------------------------------

def _call_llm_for_merge(old_decision: str, new_decision: str, config_data: dict) -> str:
    """Call the configured LLM to propose a unified decision."""
    prompt = f"""You are an architectural decision merger. You have two conflicting decisions about the same module.

Old decision:
{old_decision}

New decision:
{new_decision}

Propose a single unified decision that combines the best of both, resolving any contradictions.
Return ONLY the unified decision text, no explanation, no markdown, no JSON.
"""
    model = config_data.get('model', 'claude-sonnet-4-6')
    is_anthropic_model = model.startswith('claude-') or model.startswith('anthropic/')
    
    try:
        if is_anthropic_model and 'api_key' in config_data and not any(k in config_data for k in ['openai_key', 'openrouter_key', 'gemini_key', 'groq_key']):
            from anthropic import Anthropic
            client = Anthropic(api_key=config_data['api_key'])
            actual_model = model.replace('anthropic/', '')
            response = client.messages.create(
                model=actual_model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        elif is_anthropic_model and 'openrouter_key' in config_data:
            url = "https://openrouter.ai/api/v1/chat/completions"
            key = config_data['openrouter_key']
        else:
            if model.startswith('gpt-') or model.startswith('o1') or model.startswith('o3'):
                if 'openai_key' not in config_data:
                    raise ValueError("OpenAI API key missing.")
                url = "https://api.openai.com/v1/chat/completions"
                key = config_data['openai_key']
            elif model.startswith('gemini-') or model.startswith('gemma-'):
                if 'gemini_key' not in config_data:
                    raise ValueError("Gemini API key missing.")
                url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                key = config_data['gemini_key']
            elif '/' in model:
                if 'openrouter_key' not in config_data:
                    raise ValueError("OpenRouter API key missing.")
                url = "https://openrouter.ai/api/v1/chat/completions"
                key = config_data['openrouter_key']
            elif config_data.get('groq_key'):
                url = "https://api.groq.com/openai/v1/chat/completions"
                key = config_data['groq_key']
            elif config_data.get('openai_key'):
                url = "https://api.openai.com/v1/chat/completions"
                key = config_data['openai_key']
            elif config_data.get('openrouter_key'):
                url = "https://openrouter.ai/api/v1/chat/completions"
                key = config_data['openrouter_key']
            else:
                raise ValueError(f"No valid API key configured for model: {model}")
            
            data = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}]
            }).encode('utf-8')
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
            if "openrouter.ai" in url:
                headers["HTTP-Referer"] = "https://github.com/getKlyd/klyd"
                headers["X-Title"] = "klyd"
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req) as response:
                res = json.loads(response.read().decode('utf-8'))
                return res['choices'][0]['message']['content'].strip()
    except Exception as e:
        console.print(f"[red]LLM merge suggestion failed: {e}[/red]")
        return None

# ----------------------------------------------------------------------
# Diff display helper
# ----------------------------------------------------------------------

def _format_diff(old_text: str, new_text: str):
    """Build a side-by-side diff table.

    Returning a renderable (rather than one pre-joined string) lets Rich divide
    the width between the two sides and fold each line. The old implementation
    padded both sides to 40 characters and joined them into a 83-character
    line, which wrapped at an 80-column terminal and read as garbled output.
    """
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(overflow="fold")
    table.add_column(overflow="fold")

    old_lines = old_text.split('\n')
    new_lines = new_text.split('\n')
    max_len = max(len(old_lines), len(new_lines))

    table.add_row("[bold red]Old[/bold red]", "[bold green]New[/bold green]")
    for i in range(max_len):
        old_line = old_lines[i] if i < len(old_lines) else ''
        new_line = new_lines[i] if i < len(new_lines) else ''
        if old_line != new_line:
            table.add_row(f"[red]{old_line}[/red]", f"[green]{new_line}[/green]")
        else:
            table.add_row(f"[dim]{old_line}[/dim]", f"[dim]{new_line}[/dim]")
    return table

# ----------------------------------------------------------------------
# Status helpers
# ----------------------------------------------------------------------

def _compute_stats(active, flagged):
    total = len(active) + len(flagged)
    conflict_rate = len(flagged) / total if total > 0 else 0.0
    drift_score = conflict_rate
    return {
        'total': total,
        'active': len(active),
        'flagged': len(flagged),
        'conflict_rate': conflict_rate,
        'drift_score': drift_score
    }

def _build_module_tree(decisions, pinned_ids):
    """Build a rich Tree grouped by module."""
    tree = Tree("[bold cyan]Decisions by Module[/bold cyan]")
    modules = {}
    for d in decisions:
        mod = d['module']
        if mod not in modules:
            modules[mod] = []
        modules[mod].append(d)
    
    for mod in sorted(modules.keys()):
        mod_node = tree.add(f"[bold blue]{mod}[/bold blue]")
        for d in modules[mod]:
            conf_color = "bold bright_green" if d['confidence'] == 'HIGH' else ("bold bright_yellow" if d['confidence'] == 'MEDIUM' else "dim white")
            id_str = str(d['id'])
            if d['id'] in pinned_ids:
                id_str = f"* {id_str}"
            decision_text = d['decision'][:60] + '...' if len(d['decision']) > 60 else d['decision']
            mod_node.add(f"[{conf_color}]{id_str}: {decision_text}[/{conf_color}]")
    return tree


def _json_safe_decisions(decisions):
    """Replace binary columns with null so the export is serialisable.

    `embedding` is a BLOB. It is used for ranking and is meaningless in a
    status export, but json.dumps refuses bytes -- so `kl status --format json`
    crashed as soon as a decision written by extraction had an embedding.
    Seeded rows have NULL embeddings, which is why the earlier tests missed it.
    """
    return [
        {
            key: (None if isinstance(value, (bytes, bytearray, memoryview)) else value)
            for key, value in decision.items()
        }
        for decision in decisions
    ]


def _export_status(active, flagged, stats, format_type):
    """Export status in the requested format."""
    if format_type == 'json':
        data = {
            'stats': stats,
            'active_decisions': _json_safe_decisions(active),
            'flagged_decisions': _json_safe_decisions(flagged)
        }
        return json.dumps(data, indent=2)
    elif format_type == 'markdown':
        lines = []
        lines.append("# klyd Status\n")
        lines.append(f"**Total decisions:** {stats['total']}  ")
        lines.append(f"**Active:** {stats['active']}  ")
        lines.append(f"**Flagged:** {stats['flagged']}  ")
        lines.append(f"**Conflict rate:** {stats['conflict_rate']:.2%}  ")
        lines.append(f"**Drift score:** {stats['drift_score']:.2%}  ")
        lines.append("")
        lines.append("## Active Decisions")
        lines.append("")
        lines.append("| ID | Decision | Module | Confidence | Reinforcements |")
        lines.append("|----|----------|--------|------------|----------------|")
        for d in active:
            lines.append(f"| {d['id']} | {d['decision'][:60]} | {d['module']} | {d['confidence']} | {d['reinforcement_count']} |")
        if flagged:
            lines.append("")
            lines.append("## Flagged Decisions (Needs Review)")
            lines.append("")
            lines.append("| ID | Decision | Module | Confidence |")
            lines.append("|----|----------|--------|------------|")
            for d in flagged:
                lines.append(f"| {d['id']} | {d['decision'][:60]} | {d['module']} | {d['confidence']} |")
        return '\n'.join(lines)
    elif format_type == 'html':
        lines = []
        lines.append("<html><head><title>klyd Status</title></head><body>")
        lines.append("<h1>klyd Status</h1>")
        lines.append(f"<p><b>Total decisions:</b> {stats['total']}</p>")
        lines.append(f"<p><b>Active:</b> {stats['active']}</p>")
        lines.append(f"<p><b>Flagged:</b> {stats['flagged']}</p>")
        lines.append(f"<p><b>Conflict rate:</b> {stats['conflict_rate']:.2%}</p>")
        lines.append(f"<p><b>Drift score:</b> {stats['drift_score']:.2%}</p>")
        lines.append("<h2>Active Decisions</h2>")
        lines.append("<table border='1'><tr><th>ID</th><th>Decision</th><th>Module</th><th>Confidence</th><th>Reinforcements</th></tr>")
        for d in active:
            lines.append(f"<tr><td>{d['id']}</td><td>{d['decision'][:60]}</td><td>{d['module']}</td><td>{d['confidence']}</td><td>{d['reinforcement_count']}</td></tr>")
        lines.append("</table>")
        if flagged:
            lines.append("<h2>Flagged Decisions (Needs Review)</h2>")
            lines.append("<table border='1'><tr><th>ID</th><th>Decision</th><th>Module</th><th>Confidence</th></tr>")
            for d in flagged:
                lines.append(f"<tr><td>{d['id']}</td><td>{d['decision'][:60]}</td><td>{d['module']}</td><td>{d['confidence']}</td></tr>")
            lines.append("</table>")
        lines.append("</body></html>")
        return '\n'.join(lines)
    else:
        return ""

# ----------------------------------------------------------------------
# CLI commands
# ----------------------------------------------------------------------

@cli.command()
def init():
    """Initialize klyd in the current git repository."""
    # `kl init` installs hooks in `.git/hooks` and writes `.klyd/`, both
    # relative to the working directory. Resolve the worktree root so running
    # it from a subdirectory initialises the repository, not the subdirectory.
    git_root = _find_git_root()
    if git_root is None:
        console.print("[bold red]Not a git repository.[/bold red] Run `kl init` inside a git repo.")
        raise click.exceptions.Exit(1)
    if Path.cwd().resolve() != git_root:
        os.chdir(git_root)

    logger.info("Initializing klyd")
    console.print()
    console.print(r"[green bold]888  /         888           Y88b    /       888~-_   [/green bold]")
    console.print(r"[green bold]888 /          888            Y88b  /        888   \  [/green bold]")
    console.print(r"[green bold]888/\          888             Y88b/         888    | [/green bold]")
    console.print(r"[green bold]888  \         888              Y8Y          888    | [/green bold]")
    console.print(r"[green bold]888   \        888               Y           888   /  [/green bold]")
    console.print(r"[green bold]888    \       888____          /            888_-~   [/green bold]")
    console.print()
    console.print(r"                   [dim](An open-source project, not affiliated with the Klyd SaaS)[/dim]")
    console.print()
    console.print()
    try:
        with _status("[bold cyan]Creating .klyd directory, database, and hooks...[/bold cyan]"):
            install_hooks()
            init_config()
            klyd_dir = Path('.klyd')
            db_path = klyd_dir / 'memory.db'
            from .db import init_db
            init_db(str(db_path))
            # Migration is called inside init_db, but call again for safety
            migrate_db(str(db_path))
            # Record initial metrics
            update_metrics(str(db_path))
            
        console.print(Panel(
            "Klyd harness initialised in [cyan].klyd[/cyan]\n\n[dim]Installed git hooks for automatic extraction.[/dim]\n[dim]Errors are logged to .klyd/errors.log[/dim]",
            title="[bold green]SUCCESS[/bold green]", border_style="green", padding=(1, 2)
        ))
    except Exception as e:
        logger.error("Initialization failed", extra={'error': str(e)})
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()

@cli.command()
@click.option('--api-key', help='Anthropic API key')
@click.option('--openai-key', help='OpenAI API key')
@click.option('--openrouter-key', help='OpenRouter API key')
@click.option('--gemini-key', help='Gemini API key')
@click.option('--groq-key', help='Groq API key')
@click.option('--model', help='Model to use (default: claude-sonnet-4-6)')
@click.option('--api-base', help='OpenAI-compatible base URL to use instead of the built-in provider endpoints (e.g. a local gateway)')
@click.option('--show', is_flag=True, help='Show current configuration')
@click.option('--injection-template', help='Custom injection template with {decisions} placeholder')
@click.option('--strict-mode', type=bool, help='Enable strict mode (only file-matched decisions)')
@click.option('--max-decisions', type=int, help='Maximum number of decisions to inject')
@click.option('--min-confidence', type=click.Choice(['LOW', 'MEDIUM', 'HIGH']), help='Minimum confidence level to include')
@click.option('--module-filter', help='Comma-separated list of modules to include')
def config(api_key, openai_key, openrouter_key, gemini_key, groq_key, model, api_base, show, injection_template, strict_mode, max_decisions, min_confidence, module_filter):
    """Set klyd configuration."""
    if show:
        cfg = get_all_config()
        table = Table(title="[bold cyan]klyd Configuration[/bold cyan]", box=rich.box.SIMPLE, header_style="bold cyan")
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        if not cfg:
            table.add_row("[dim]No configuration set[/dim]", "[dim]Run: kl config --api-key ...[/dim]")
        else:
            for k, v in cfg.items():
                if 'key' in k and isinstance(v, str) and v:
                    v = v[:4] + '*' * (len(v) - 8) + v[-4:] if len(v) > 8 else '*' * len(v)
                elif not isinstance(v, str):
                    # Lists (pinned_decision_ids, module_filter) and None are not
                    # renderables; str() them so --show works with default config.
                    v = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
                table.add_row(k, v)
        console.print(table)
        return

    changes = False
    with _status("[bold cyan]Saving configuration...[/bold cyan]"):
        if api_key:
            set_config('api_key', api_key)
            changes = True
        if openai_key:
            set_config('openai_key', openai_key)
            changes = True
        if openrouter_key:
            set_config('openrouter_key', openrouter_key)
            changes = True
        if gemini_key:
            set_config('gemini_key', gemini_key)
            changes = True
        if groq_key:
            set_config('groq_key', groq_key)
            changes = True
        if model:
            set_config('model', model)
            changes = True
        if api_base:
            set_config('api_base', api_base)
            changes = True
        if injection_template is not None:
            set_injection_template(injection_template)
            changes = True
        if strict_mode is not None:
            set_strict_mode(strict_mode)
            changes = True
        if max_decisions is not None:
            set_max_decisions_inject(max_decisions)
            changes = True
        if min_confidence is not None:
            set_min_confidence(min_confidence)
            changes = True
        if module_filter is not None:
            modules = [m.strip() for m in module_filter.split(',') if m.strip()]
            set_module_filter(modules)
            changes = True
            
    if changes:
        logger.info("Configuration saved")
        console.print(Panel("[bold green]Configuration saved successfully[/bold green]", title="[bold green]DONE[/bold green]", border_style="green", expand=False))
    else:
        console.print("[yellow]Usage:[/yellow] kl config --api-key ... --openai-key ... --model ... --injection-template ... --strict-mode ... --max-decisions ... --min-confidence ... --module-filter ...\nOr use --show to display current configuration.")

def _agent_command(cmd, injection_path, injection_text):
    """Return the agent argv with klyd's injection wired in.

    Aider reads a file through ``--message-file``. OpenCode takes its prompt as
    a positional argument to the ``run`` subcommand -- ``-m`` selects the
    *model*, so the previous ``extend(['-m', injection])`` told OpenCode to use
    the entire injection text as a model name and never delivered the prompt.
    """
    argv = list(cmd)
    agent = argv[0].lower()

    if agent == 'aider':
        argv.extend(['--message-file', str(injection_path)])
    elif agent == 'opencode':
        if 'run' not in argv[1:]:
            argv.insert(1, 'run')
        argv.append(injection_text)

    return argv


@cli.command(context_settings={"ignore_unknown_options": True})
@click.option('--no-inject', is_flag=True, help='Skip generating injection file')
@click.option('--relevance-mode', type=click.Choice(['balanced', 'strict']), default='balanced', help='Relevance scoring mode')
@click.argument('cmd', nargs=-1, type=click.UNPROCESSED)
def run(no_inject, relevance_mode, cmd):
    """Run an agent with injected architectural memory."""
    if not cmd:
        console.print("Usage: kl run <agent> [args...]")
        return
        
    klyd_dir = Path('.klyd')
    inj_path = klyd_dir / 'injection.txt'
    
    if not no_inject:
        with _status("[bold cyan]Preparing injection context...[/bold cyan]"):
            try:
                ctx = click.get_current_context()
                # Pass relevance_mode to prepare_injection
                ctx.invoke(prepare_injection, relevance_mode=relevance_mode)
            except Exception as e:
                pass
            
    run_cmd = list(cmd)
    agent_name = run_cmd[0].lower()
    
    if inj_path.exists() and inj_path.stat().st_size > 0:
        # Read as UTF-8 explicitly: the default locale encoding on Windows is
        # cp1252, so a decision containing non-ASCII text crashed `kl run`.
        run_cmd = _agent_command(
            run_cmd, inj_path, inj_path.read_text(encoding='utf-8')
        )
            
    console.print(Panel(f"[bold cyan]Launching[/bold cyan] {agent_name}...", title="[bold blue]AGENT START[/bold blue]", border_style="blue", expand=False))
    
    try:
        subprocess.run(run_cmd)
        console.print(Panel("[bold green]Agent session ended[/bold green]", title="[bold green]DONE[/bold green]", border_style="green", expand=False))
    except FileNotFoundError:
        console.print(f"[red]Command not found: {cmd[0]}[/red]")

@cli.command()
def extract_commit():
    """Extract decisions from the last commit."""
    from .extractor import extract_decisions
    from .db import get_decisions_for_files, store_decision_with_embedding, reinforce_decision, flag_decision, update_metrics
    
    klyd_dir = Path('.klyd')
    if not (klyd_dir / 'memory.db').exists():
        return
    
    try:
        diff = subprocess.check_output(['git', 'show', 'HEAD'], text=True)
        files_out = subprocess.check_output(['git', 'show', '--name-only', '--format=', 'HEAD'], text=True)
        msg = subprocess.check_output(['git', 'log', '-1', '--format=%B'], text=True)
        files = [f for f in files_out.strip().split('\n') if f]
        commit_hash = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    except subprocess.CalledProcessError as e:
        err_log = klyd_dir / 'errors.log'
        with open(err_log, 'a') as f:
            f.write(f"Error running git command: {e}\n")
        return

    if not files:
        return

    try:
        with _status("[bold cyan]Extracting architectural decisions via LLM...[/bold cyan]"):
            db_path = str(klyd_dir / 'memory.db')
            existing = get_decisions_for_files(db_path, files, top_k=20)
            existing_json = json.dumps(_json_safe_decisions(existing), indent=2)

            config_data = get_all_config()
            model = config_data.get('model', 'claude-sonnet-4-6')
            
            decisions = extract_decisions(diff, msg, existing_json, config_data, model)

            new_count = 0
            reinforced_count = 0

            for d in decisions:
                event = d.get('event_type')
                d['last_seen_commit'] = commit_hash
                emb_bytes = d.pop('embedding_bytes', None)
                
                if event == 'REINFORCE':
                    match = next((e for e in existing if e['module'] == d['module'] and e['decision'] == d['decision']), None)
                    if match:
                        reinforce_decision(db_path, match['id'], commit_hash)
                        reinforced_count += 1
                    else:
                        d['event_type'] = 'NEW'
                        store_decision_with_embedding(db_path, d, embedding_bytes=emb_bytes)
                        new_count += 1
                elif event == 'CONTRADICT':
                    did = store_decision_with_embedding(db_path, d, embedding_bytes=emb_bytes)
                    flag_decision(db_path, did)
                    new_count += 1
                else:
                    store_decision_with_embedding(db_path, d, embedding_bytes=emb_bytes)
                    new_count += 1
            
            # Update metrics after extraction
            update_metrics(db_path)
                    
        if new_count > 0 or reinforced_count > 0:
            console.print(Panel(
                f"[bold green]{new_count}[/bold green] new decisions extracted\n[bold yellow]{reinforced_count}[/bold yellow] decisions reinforced",
                title="[bold cyan]EXTRACTION COMPLETE[/bold cyan]", border_style="cyan", expand=False
            ))
        else:
            console.print(Panel(
                "[dim]No architectural decisions found in this commit[/dim]",
                title="[dim]EXTRACTION COMPLETE[/dim]", border_style="dim", expand=False
            ))

    except Exception as e:
        err_log = klyd_dir / 'errors.log'
        with open(err_log, 'a', encoding='utf-8') as f:
            f.write(f"Error extracting commit:\n{traceback.format_exc()}\n")
        # Do not swallow this: without a message, a user with no API key sees
        # only "ERROR: Extraction failed" and has no way to know why.
        console.print(f"[bold red]Extraction failed:[/bold red] {e}")
        console.print("[dim]Check `kl config --show`, then set a key with `kl config --api-key ...`.[/dim]")
        raise click.exceptions.Exit(1)

@cli.command()
@click.option('--relevance-mode', type=click.Choice(['balanced', 'strict']), default='balanced', help='Relevance scoring mode')
@click.option('--min-confidence', type=click.Choice(['LOW', 'MEDIUM', 'HIGH']), default=None, help='Minimum confidence level')
@click.option('--module-filter', default=None, help='Comma-separated list of modules to include')
@click.option('--template', default=None, help='Custom injection template')
def prepare_injection(relevance_mode, min_confidence, module_filter, template):
    """Prepare injection file for agent sessions."""
    from .db import get_decisions_for_files
    from .injector import format_injection
    
    klyd_dir = Path('.klyd')
    if not (klyd_dir / 'memory.db').exists():
        return
        
    try:
        with _status("[bold cyan]Preparing injection context...[/bold cyan]"):
            files_out = subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True)
            files = [f for f in files_out.strip().split('\n') if f]
            
            if not files:
                with open(klyd_dir / 'injection.txt', 'w') as f:
                    f.write('')
                return
                
            db_path = str(klyd_dir / 'memory.db')
            decisions = get_decisions_for_files(db_path, files, top_k=20)
            
            task_description = None
            
            # Parse module_filter
            module_list = None
            if module_filter:
                module_list = [m.strip() for m in module_filter.split(',') if m.strip()]
            
            injection = format_injection(
                decisions,
                db_path=db_path,
                task_description=task_description,
                relevance_mode=relevance_mode,
                top_k=None,
                template=template,
                min_confidence=min_confidence,
                module_filter=module_list,
                preview=False
            )
            with open(klyd_dir / 'injection.txt', 'w') as f:
                f.write(injection)
                
        console.print(Panel("[green]Injection file ready at .klyd/injection.txt[/green]", border_style="green", expand=False))
            
    except Exception as e:
        with open(klyd_dir / 'errors.log', 'a') as f:
            f.write(f"Error preparing injection:\n{traceback.format_exc()}\n")
        return

@cli.command()
@click.option('--relevance-mode', type=click.Choice(['balanced', 'strict']), default='balanced', help='Relevance scoring mode')
@click.option('--min-confidence', type=click.Choice(['LOW', 'MEDIUM', 'HIGH']), default=None, help='Minimum confidence level')
@click.option('--module-filter', default=None, help='Comma-separated list of modules to include')
@click.option('--template', default=None, help='Custom injection template')
def preview_injection(relevance_mode, min_confidence, module_filter, template):
    """Preview the injection file without writing it."""
    from .db import get_decisions_for_files
    from .injector import format_injection
    
    klyd_dir = Path('.klyd')
    if not (klyd_dir / 'memory.db').exists():
        console.print("[bold red]klyd is not initialized. Run `kl init`.[/bold red]")
        return
        
    try:
        with _status("[bold cyan]Generating preview...[/bold cyan]"):
            files_out = subprocess.check_output(['git', 'diff', '--cached', '--name-only'], text=True)
            files = [f for f in files_out.strip().split('\n') if f]
            
            if not files:
                console.print("[yellow]No staged files. Injection would be empty.[/yellow]")
                return
                
            db_path = str(klyd_dir / 'memory.db')
            decisions = get_decisions_for_files(db_path, files, top_k=20)
            
            task_description = None
            
            module_list = None
            if module_filter:
                module_list = [m.strip() for m in module_filter.split(',') if m.strip()]
            
            injection = format_injection(
                decisions,
                db_path=db_path,
                task_description=task_description,
                relevance_mode=relevance_mode,
                top_k=None,
                template=template,
                min_confidence=min_confidence,
                module_filter=module_list,
                preview=True
            )
            
    except Exception as e:
        console.print(f"[red]Error generating preview: {e}[/red]")
        return

@cli.command()
@click.argument('decision_id', type=int)
def pin(decision_id):
    """Pin a decision so it always appears in injection."""
    from .db import get_decision_by_id
    klyd_dir = Path('.klyd')
    db_path = klyd_dir / 'memory.db'
    if not db_path.exists():
        console.print("[bold red]klyd is not initialized. Run `kl init`.[/bold red]")
        return
    
    decision = get_decision_by_id(str(db_path), decision_id)
    if not decision:
        console.print(f"[red]Decision with id {decision_id} not found.[/red]")
        raise click.exceptions.Exit(1)

    add_pinned_decision_id(decision_id)
    logger.info("Pinned decision", extra={'decision_id': decision_id})
    text = decision['decision']
    if len(text) > 60:
        text = text[:60] + "..."
    console.print(Panel(
        f"[bold green]Pinned decision {decision_id}:[/bold green] {text}",
        border_style="green", expand=False
    ))

@cli.command()
@click.argument('decision_id', type=int, required=False)
def unpin(decision_id):
    """Unpin a decision. If no id given, unpin all."""
    if decision_id is None:
        clear_pinned_decision_ids()
        logger.info("Unpinned all decisions")
        console.print("[bold yellow]All decisions unpinned.[/bold yellow]")
    else:
        remove_pinned_decision_id(decision_id)
        logger.info("Unpinned decision", extra={'decision_id': decision_id})
        console.print(f"[bold yellow]Unpinned decision {decision_id}.[/bold yellow]")

@cli.command()
def review():
    """Review flagged conflicting decisions with enhanced conflict resolution."""
    from .db import get_flagged_decisions, get_active_decisions_by_module, resolve_decision, update_metrics
    
    klyd_dir = Path('.klyd')
    db_path = klyd_dir / 'memory.db'
    if not db_path.exists():
        console.print("[bold red]klyd is not initialized. Run `kl init`.[/bold red]")
        return

    db_str = str(db_path)
    flagged = get_flagged_decisions(db_str)
    
    if not flagged:
        console.print(Panel(
            "[bold green]All conflicts resolved. Memory is clean.[/bold green]",
            title="[bold green]NO CONFLICTS[/bold green]", border_style="green", expand=False
        ))
        return

    config_data = get_all_config()

    for i, d in enumerate(flagged):
        console.print()
        console.print(f"[bold cyan]Conflict {i+1} of {len(flagged)}[/bold cyan] in module: [bold white]{d['module']}[/bold white]")
        
        commit_ref = d.get('last_seen_commit') or "unknown commit"
        
        active = get_active_decisions_by_module(db_str, d['module'])
        old_id = None
        old_decision_text = ""
        if active:
            old = active[0]
            old_id = old['id']
            old_decision_text = old['decision']
            conf_color = "bold bright_green" if old['confidence'] == 'HIGH' else ("bold bright_yellow" if old['confidence'] == 'MEDIUM' else "dim white")
            old_panel = Panel(
                f"{old['decision']}\n\n[dim]([/dim][{conf_color}]{old['confidence']}[/{conf_color}][dim] | reinforced x{old['reinforcement_count']})[/dim]",
                title="[bold yellow]Existing Memory[/bold yellow]",
                border_style="yellow",
                expand=True
            )
        else:
            old_panel = Panel(
                "\n[dim]No existing memory found.[/dim]\n",
                title="[bold yellow]Existing Memory[/bold yellow]",
                border_style="yellow",
                expand=True
            )

        new_conf_color = "bold bright_green" if d['confidence'] == 'HIGH' else ("bold bright_yellow" if d['confidence'] == 'MEDIUM' else "dim white")
        new_panel = Panel(
            f"{d['decision']}\n\n[dim]([/dim][{new_conf_color}]{d['confidence']}[/{new_conf_color}][dim] | commit {commit_ref[:7]})[/dim]",
            title="[bold red]New Conflicting Extraction[/bold red]",
            border_style="red",
            expand=True
        )

        console.print(Columns([old_panel, new_panel]))
        
        # Show side-by-side diff
        console.print()
        console.print("[bold cyan]Side-by-side diff:[/bold cyan]")
        diff_text = _format_diff(old_decision_text, d['decision'])
        console.print(Panel(diff_text, title="[bold]Old | New[/bold]", border_style="blue", expand=False))
        
        console.print()
        console.print("  [bold green][ a ][/bold green] Accept+Merge (LLM suggests unified decision)")
        console.print("  [bold yellow][ o ][/bold yellow] ArchiveOld+AcceptNew (keep new, archive old)")
        console.print("  [bold blue][ e ][/bold blue] ManualEdit (edit the new decision)")
        console.print("  [bold red][ r ][/bold red] Reject new (keep old)")
        console.print("  [bold magenta][ w ][/bold magenta] AutoArchiveWeak (archive old if weak confidence)")
        console.print("  [bold dim][ s ][/bold dim] Skip for now")
        console.print()
        
        while True:
            choice = Prompt.ask("[bold cyan]Select action[/bold cyan]", choices=["a", "o", "e", "r", "w", "s"], show_choices=False).lower()
            if choice == 's':
                console.print("[dim]Skipped.[/dim]")
                break
            elif choice == 'a':
                # Accept+Merge: call LLM to suggest unified decision
                with _status("[bold cyan]Asking LLM to suggest a merge...[/bold cyan]"):
                    merged_text = _call_llm_for_merge(old_decision_text, d['decision'], config_data)
                if merged_text:
                    console.print(Panel(
                        f"[bold green]LLM suggests:[/bold green]\n{merged_text}",
                        title="[bold]Merge Suggestion[/bold]",
                        border_style="green",
                        expand=False
                    ))
                    confirm = Prompt.ask("[bold cyan]Accept this merge?[/bold cyan]", choices=["y", "n"], default="y")
                    if confirm == 'y':
                        # Create a new version from the old decision with the merged text
                        new_version_id = create_decision_version(db_str, old_id, {
                            'decision': merged_text,
                            'confidence': 'MEDIUM',
                            'event_type': 'NEW',
                            'last_seen_commit': d.get('last_seen_commit')
                        })
                        # Archive the old decision
                        merge_decisions(db_str, new_version_id, old_id)
                        # Archive the new conflicting decision
                        merge_decisions(db_str, new_version_id, d['id'])
                        # Update metrics
                        update_metrics(db_str)
                        console.print(Panel(
                            "[bold green]Merged decision created. Old and conflicting decisions archived.[/bold green]",
                            border_style="green", expand=False
                        ))
                        break
                    else:
                        console.print("[dim]Merge cancelled. Choose another option.[/dim]")
                        continue
                else:
                    console.print("[red]LLM merge failed. Please choose another option.[/red]")
                    continue
            elif choice == 'o':
                # ArchiveOld+AcceptNew
                if old_id:
                    merge_decisions(db_str, d['id'], old_id)
                resolve_decision(db_str, d['id'], 'accept', old_id=old_id)
                update_metrics(db_str)
                console.print(Panel(
                    "[bold yellow]Old decision archived. New decision accepted.[/bold yellow]",
                    border_style="yellow", expand=False
                ))
                break
            elif choice == 'e':
                new_text = click.edit(d['decision'])
                if new_text is not None:
                    new_text = new_text.strip()
                    resolve_decision(db_str, d['id'], 'edit', old_id=old_id, new_text=new_text)
                    update_metrics(db_str)
                    console.print(Panel(
                        "[bold blue]Saved edited decision. Memory updated.[/bold blue]",
                        border_style="blue", expand=False
                    ))
                    break
                else:
                    console.print("[red]Edit cancelled. Please choose an option.[/red]")
            elif choice == 'r':
                resolve_decision(db_str, d['id'], 'reject')
                update_metrics(db_str)
                console.print(Panel(
                    "[bold red]Rejected new decision. Existing memory preserved.[/bold red]",
                    border_style="red", expand=False
                ))
                break
            elif choice == 'w':
                # AutoArchiveWeak: archive old if its confidence is LOW
                if old_id:
                    old_decision = get_decision_by_id(db_str, old_id)
                    if old_decision and old_decision['confidence'] == 'LOW':
                        merge_decisions(db_str, d['id'], old_id)
                        resolve_decision(db_str, d['id'], 'accept', old_id=old_id)
                        update_metrics(db_str)
                        console.print(Panel(
                            "[bold magenta]Old decision (LOW confidence) archived. New decision accepted.[/bold magenta]",
                            border_style="magenta", expand=False
                        ))
                    else:
                        console.print("[yellow]Old decision confidence is not LOW. Skipping auto-archive.[/yellow]")
                        continue
                else:
                    console.print("[yellow]No old decision to archive.[/yellow]")
                    continue
                break

@cli.command()
@click.option('--search', default=None, help='Search keyword to filter decisions')
@click.option('--tree', is_flag=True, help='Show tree view by module')
@click.option('--format', 'format_type', type=click.Choice(['table', 'tree', 'markdown', 'html', 'json']), default='table', help='Output format')
@click.option('--stats', is_flag=True, help='Show drift/conflict stats')
def status(search, tree, format_type, stats):
    """Show the current memory store status."""
    klyd_dir = Path('.klyd')
    db_path = klyd_dir / 'memory.db'
    if not db_path.exists():
        console.print("[bold red]klyd is not initialized. Run `kl init`.[/bold red]")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM decisions WHERE archived = 0")
    all_decisions = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Apply search filter
    if search:
        search_lower = search.lower()
        all_decisions = [d for d in all_decisions if search_lower in d['decision'].lower() or search_lower in d['module'].lower()]

    active = [d for d in all_decisions if d['flagged'] == 0]
    flagged = [d for d in all_decisions if d['flagged'] == 1]
    
    active.sort(key=lambda x: x['reinforcement_count'], reverse=True)

    pinned_ids = get_pinned_decision_ids()

    # Compute stats
    stats_data = _compute_stats(active, flagged)

    # If --stats flag is set, show stats panel
    if stats:
        stats_panel = Panel(
            f"[bold]Total decisions:[/bold] {stats_data['total']}\n"
            f"[bold]Active:[/bold] {stats_data['active']}\n"
            f"[bold]Flagged:[/bold] {stats_data['flagged']}\n"
            f"[bold]Conflict rate:[/bold] {stats_data['conflict_rate']:.2%}\n"
            f"[bold]Drift score:[/bold] {stats_data['drift_score']:.2%}",
            title="[bold cyan]Memory Stats[/bold cyan]",
            border_style="cyan",
            expand=False
        )
        console.print(stats_panel)
        console.print()

    # Handle export formats
    if format_type in ('markdown', 'html', 'json'):
        export_text = _export_status(active, flagged, stats_data, format_type)
        # These formats are consumed by machines or pasted into files, so they
        # must pass through byte-for-byte: Rich would word-wrap long lines (which
        # puts literal newlines inside JSON string values and makes the output
        # unparseable) and interpret HTML/markdown as console markup.
        console.print(export_text, soft_wrap=True, markup=False, highlight=False)
        return

    # If --tree flag is set, show tree view
    if tree or format_type == 'tree':
        tree_view = _build_module_tree(all_decisions, pinned_ids)
        console.print(tree_view)
        return

    if not active:
        # Do not put the empty state inside the table: with six columns and one
        # long cell, Rich squeezes the Decision column and wraps the sentence
        # character by character. This is the first screen a new user sees.
        console.print(
            Panel(
                "[dim]No active architectural decisions stored yet.\n"
                "Make a commit, then run `kl extract-commit`, or `kl review`.[/dim]",
                title="[bold cyan] Architectural Memory [/bold cyan]",
                border_style="cyan",
                expand=False,
                padding=(1, 2),
            )
        )
    else:
        # Six side-by-side columns do not fit in an 80-column terminal: Rich
        # squeezes every column to its minimum, which truncates headers
        # ("Reinforc...") and wraps the decision text one word per line. Lay
        # each decision out as a block instead -- a short metadata line above
        # the decision -- so the text has the full panel width and stays
        # readable at any terminal size.
        body = Table.grid(padding=(0, 1), expand=False)
        body.add_column(no_wrap=True, width=4, style="dim", justify="right")
        body.add_column(overflow="fold")

        for index, d in enumerate(active):
            if index:
                body.add_row("", "")

            conf_color = "bold bright_green" if d['confidence'] == 'HIGH' else ("bold bright_yellow" if d['confidence'] == 'MEDIUM' else "dim white")
            conf_styled = f"[{conf_color}]{d['confidence']}[/{conf_color}]"

            event_color = "bright_blue" if d.get('event_type') == 'NEW' else ("bright_magenta" if d.get('event_type') == 'REINFORCE' else "bold bright_red")
            event_styled = f"[{event_color}]{d.get('event_type', 'NEW')}[/{event_color}]"

            marker = "[bold yellow]*[/bold yellow] " if d['id'] in pinned_ids else ""
            meta = (
                f"[bold blue]{d['module']}[/bold blue]  "
                f"{conf_styled}  {event_styled}  "
                f"[dim]x{d['reinforcement_count']}[/dim]"
            )

            body.add_row(f"{marker}[dim]#{d['id']}[/dim]", meta)
            body.add_row("", Text(d['decision'], style="white"))

        console.print(Panel(body, title="[bold cyan] Architectural Memory [/bold cyan]", border_style="cyan", expand=False, padding=(1, 2)))

    if flagged:
        console.print()
        flagged_body = Table.grid(padding=(0, 1), expand=False)
        flagged_body.add_column(no_wrap=True, width=4, style="dim", justify="right")
        flagged_body.add_column(overflow="fold")

        for index, d in enumerate(flagged):
            if index:
                flagged_body.add_row("", "")
            conf_color = "bold bright_green" if d['confidence'] == 'HIGH' else ("bold bright_yellow" if d['confidence'] == 'MEDIUM' else "dim white")
            conf_styled = f"[{conf_color}]{d['confidence']}[/{conf_color}]"

            flagged_body.add_row(
                f"[dim]#{d['id']}[/dim]",
                f"[bold blue]{d['module']}[/bold blue]  {conf_styled}",
            )
            flagged_body.add_row("", Text(d['decision'], style="bold white"))

        warning_panel = Panel(
            flagged_body,
            title="[bold white on red] NEEDS REVIEW - CONFLICTS DETECTED [/bold white on red]",
            border_style="red",
            expand=False,
            padding=(1, 2)
        )
        console.print(warning_panel)

    console.print()
    console.print(f"[dim]Summary:[/dim] [cyan]{len(active)} active[/cyan] | [red]{len(flagged)} pending review[/red]")

@cli.command()
def metrics():
    """Show a simple text summary of tracked metrics."""
    klyd_dir = Path('.klyd')
    db_path = klyd_dir / 'memory.db'
    if not db_path.exists():
        console.print("[bold red]klyd is not initialized. Run `kl init`.[/bold red]")
        return

    # Update metrics first
    update_metrics(str(db_path))
    summary = get_metrics_summary(str(db_path))

    panel = Panel(
        f"[bold]Decision count:[/bold] {int(summary['decision_count'])}\n"
        f"[bold]Average confidence:[/bold] {summary['avg_confidence']:.2f} (0=LOW, 1=MEDIUM, 2=HIGH)\n"
        f"[bold]Conflict rate:[/bold] {summary['conflict_rate']:.2%}",
        title="[bold cyan]klyd Metrics[/bold cyan]",
        border_style="cyan",
        expand=False
    )
    console.print(panel)


if __name__ == "__main__":
    # Allows `python -m klyd.cli`, which is how the git hooks invoke klyd when
    # no `kl` console script is reachable on PATH.
    cli()
