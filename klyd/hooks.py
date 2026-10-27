import os
import shutil
import stat
import sys
from pathlib import Path
from .logger import setup_logger

logger = setup_logger(__name__)

# Placeholder in the hook templates, replaced at install time with an absolute
# invocation of this installation.
LAUNCHER_TOKEN = "{{KLYD}}"


def get_template_path(hook_name):
    # Templates ship inside the package so installed wheels have them; the old
    # repo-root lookup only worked when running from a source checkout.
    hook_path = Path(__file__).resolve().parent / 'hook_templates' / f"{hook_name}.sh"
    return hook_path


def _shell_quote(value: str) -> str:
    """Single-quote a value for POSIX sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def klyd_launcher():
    """Return the command the hooks should use to reach this installation.

    Git runs hooks with whatever PATH the git process had. If klyd was installed
    into a virtualenv, with pipx, or is being used from a GUI git client, `kl`
    is not on that PATH, and the old bare `kl` invocation failed with
    "kl: command not found" straight into .klyd/errors.log -- so the automatic
    extraction silently did nothing. Resolve it once, at install time, and bake
    the absolute path into the hook.
    """
    exe = shutil.which("kl")
    if exe:
        return _shell_quote(Path(exe).as_posix())
    # No console script on PATH: fall back to the interpreter that is running
    # this command, which by definition has klyd importable.
    return f"{_shell_quote(Path(sys.executable).as_posix())} -m klyd.cli"


def render_hook(hook_name):
    """Read a hook template and substitute the launcher."""
    src = get_template_path(hook_name)
    if not src.exists():
        raise RuntimeError(f"Hook template not found: {src}")
    template = src.read_text(encoding="utf-8")
    return template.replace(LAUNCHER_TOKEN, klyd_launcher())


def install_hooks():
    git_dir = Path('.git')
    if not git_dir.is_dir():
        raise RuntimeError("Not a git repository. Please run in the root of a git repo.")

    git_hooks_dir = git_dir / 'hooks'
    git_hooks_dir.mkdir(exist_ok=True)

    for hook in ['post-commit', 'pre-commit']:
        dest = git_hooks_dir / hook
        # Write the rendered hook explicitly: LF endings (git sh on Windows
        # chokes on CRLF) and UTF-8, rather than copying the template bytes.
        dest.write_text(render_hook(hook), encoding="utf-8", newline="\n")

        # chmod +x
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
        logger.info(f"Installed hook: {hook}", extra={'src': str(get_template_path(hook)), 'dest': str(dest)})

def uninstall_hooks():
    git_dir = Path('.git')
    if not git_dir.is_dir():
        return

    git_hooks_dir = git_dir / 'hooks'
    for hook in ['post-commit', 'pre-commit']:
        dest = git_hooks_dir / hook
        if dest.exists():
            # Read as bytes to avoid UnicodeDecodeError on binary files.
            # Match on the commands the template emits, not on the string
            # "klyd": the launcher is an absolute path that may not contain it.
            content = dest.read_bytes()
            if b"extract-commit" in content or b"prepare-injection" in content or b"klyd" in content:
                dest.unlink()
                logger.info(f"Uninstalled hook: {hook}", extra={'path': str(dest)})
