import logging
import json
import sys
from pathlib import Path

LOG_FILENAME = 'klyd.log'


def _find_log_path(start=None):
    """Return `<nearest project>/.klyd/klyd.log`, or None if there is no project.

    Resolved per-emit rather than at import time: the old module-level
    `Path('.klyd')` ran `mkdir()` during import, so merely invoking `kl` from
    any directory created a stray `.klyd/` there -- and then `kl` treated that
    accidental directory as a project root. Long-lived processes (and `kl run`,
    which can change directory) also need the path resolved when a record is
    written, not when the module is loaded.
    """
    current = Path(start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        klyd_dir = candidate / '.klyd'
        if (klyd_dir / 'memory.db').exists() or (klyd_dir / 'config.json').exists():
            return klyd_dir / LOG_FILENAME
    return None


class LazyFileHandler(logging.Handler):
    """FileHandler that attaches only once a klyd project exists."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self._handler = None

    def emit(self, record):
        if self._handler is None:
            path = _find_log_path()
            if path is None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = logging.FileHandler(str(path), mode='a', encoding='utf-8')
            self._handler.setFormatter(self.formatter)
        self._handler.emit(record)


def setup_logger(name: str) -> logging.Logger:
    """Set up a structured logger that writes to .klyd/klyd.log and stderr."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    class JsonFormatter(logging.Formatter):
        # Attributes every LogRecord carries; anything else came from an
        # `extra={...}` call and is worth keeping. The previous version looked
        # for `record.extra`, which logging never sets, so every structured
        # field (decision_id, db_path, error) was silently dropped.
        _RESERVED = {
            'name', 'msg', 'args', 'levelname', 'levelno', 'pathname', 'filename',
            'module', 'exc_info', 'exc_text', 'stack_info', 'lineno', 'funcName',
            'created', 'msecs', 'relativeCreated', 'thread', 'threadName',
            'processName', 'process', 'message', 'asctime', 'taskName',
        }

        def format(self, record):
            log_entry = {
                'timestamp': self.formatTime(record, self.datefmt),
                'level': record.levelname,
                'module': record.name,
                'message': record.getMessage(),
            }
            for key, value in record.__dict__.items():
                if key not in self._RESERVED and not key.startswith('_'):
                    log_entry[key] = value
            return json.dumps(log_entry, default=str)

    fh = LazyFileHandler()
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(JsonFormatter())
    logger.addHandler(fh)

    # Stderr handler – simple text
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logger.addHandler(sh)

    return logger
