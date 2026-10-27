import sqlite3
import fnmatch
import json
import time
import hashlib
from pathlib import Path
from .logger import setup_logger

logger = setup_logger(__name__)

def get_schema_path():
    # Lives inside the package so it survives `pip install` (see pyproject
    # package-data). The old repo-root layout only worked for `pip install -e`.
    return Path(__file__).resolve().parent / 'schema' / 'v1.sql'

def init_db(db_path):
    logger.info("Initializing database", extra={'db_path': db_path})
    conn = sqlite3.connect(db_path)
    schema = get_schema_path().read_text()
    conn.executescript(schema)
    conn.commit()
    conn.close()
    migrate_db(db_path)  # ensure new columns exist (safe for existing DBs)
    # Create metrics table if not exists
    _ensure_metrics_table(db_path)
    logger.info("Database initialized", extra={'db_path': db_path})

def _ensure_metrics_table(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def migrate_db(db_path):
    """Add new columns if they don't exist (safe migration)."""
    logger.debug("Running migration", extra={'db_path': db_path})
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Check existing columns
    cur.execute("PRAGMA table_info(decisions)")
    existing = {row[1] for row in cur.fetchall()}
    additions = []
    if 'embedding' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN embedding BLOB DEFAULT NULL")
    if 'last_reinforced_at' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN last_reinforced_at TEXT DEFAULT NULL")
    if 'relevance_score' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN relevance_score REAL DEFAULT 0.0")
    if 'version_id' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN version_id INTEGER DEFAULT 1")
    if 'parent_decision_id' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN parent_decision_id INTEGER DEFAULT NULL")
    if 'merged_into_id' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN merged_into_id INTEGER DEFAULT NULL")
    if 'auto_archive_after' not in existing:
        additions.append("ALTER TABLE decisions ADD COLUMN auto_archive_after INTEGER DEFAULT NULL")
    for stmt in additions:
        cur.execute(stmt)
        logger.debug("Added column", extra={'stmt': stmt})
    conn.commit()
    conn.close()
    _ensure_metrics_table(db_path)

def _compute_dummy_embedding(text: str) -> bytes:
    """Placeholder: returns a fixed-size byte array (128 zeros) for now.
    Replace with real embedding computation later."""
    return b'\x00' * 128

def compute_embedding(text: str) -> bytes:
    """Compute embedding for a decision text.
    Currently returns a dummy embedding; override with real model."""
    return _compute_dummy_embedding(text)


def get_embedding_for_decision(db_path: str, decision_id: int):
    """Return the stored embedding blob for a decision, or None.

    injector.py scores semantic similarity against this; the column is currently
    populated with the dummy vector from compute_embedding(), so the semantic
    term is inert until a real model replaces it. Returning None (rather than a
    zero vector) keeps that distinction visible to the caller.
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT embedding FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return None
    return row[0]


def get_reinforcement_recency_score(db_path: str, decision_id: int, half_life_days: float = 30.0) -> float:
    """Return a 0..1 recency score for a decision.

    Decays with the time since it was last reinforced, halving every
    `half_life_days`. A decision touched today scores 1.0; one untouched for a
    year scores ~0.0002. Unknown timestamps score 0.0 rather than a default,
    so a missing row cannot outrank a fresh one.
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT (julianday('now') - julianday(COALESCE(last_reinforced_at, created_at))) "
            "FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or row[0] is None:
        return 0.0
    age_days = max(0.0, float(row[0]))
    return 0.5 ** (age_days / half_life_days)

def store_decision(db_path, decision_dict):
    logger.debug("Storing decision", extra={'decision': decision_dict.get('decision', '')[:60]})
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute('''
            INSERT INTO decisions (decision, module, file_patterns, confidence, event_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            decision_dict['decision'],
            decision_dict['module'],
            decision_dict['file_patterns'],
            decision_dict['confidence'],
            decision_dict.get('event_type', 'NEW')
        ))
        conn.commit()
        decision_id = cur.lastrowid
        logger.info("New decision stored", extra={'decision_id': decision_id})
    except sqlite3.IntegrityError:
        # Duplicate decision+module – update existing row
        cur.execute('''
            UPDATE decisions
            SET file_patterns = ?,
                confidence = ?,
                event_type = ?,
                reinforcement_count = reinforcement_count + 1,
                last_seen_commit = ?,
                last_reinforced_at = datetime('now')
            WHERE decision = ? AND module = ?
        ''', (
            decision_dict['file_patterns'],
            decision_dict['confidence'],
            decision_dict.get('event_type', 'NEW'),
            decision_dict.get('last_seen_commit'),
            decision_dict['decision'],
            decision_dict['module']
        ))
        conn.commit()
        cur.execute('SELECT id FROM decisions WHERE decision = ? AND module = ?',
                    (decision_dict['decision'], decision_dict['module']))
        decision_id = cur.fetchone()[0]
        logger.info("Decision updated (duplicate)", extra={'decision_id': decision_id})
    conn.close()
    return decision_id

def store_decision_with_embedding(db_path, decision_dict, embedding_bytes=None):
    """Store a decision with an optional embedding vector.
    If embedding_bytes is None, compute it from decision text."""
    if embedding_bytes is None:
        embedding_bytes = compute_embedding(decision_dict['decision'])
    logger.debug("Storing decision with embedding", extra={'decision': decision_dict.get('decision', '')[:60]})
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        cur.execute('''
            INSERT INTO decisions (decision, module, file_patterns, confidence, event_type, embedding)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            decision_dict['decision'],
            decision_dict['module'],
            decision_dict['file_patterns'],
            decision_dict['confidence'],
            decision_dict.get('event_type', 'NEW'),
            embedding_bytes
        ))
        conn.commit()
        decision_id = cur.lastrowid
        logger.info("New decision stored with embedding", extra={'decision_id': decision_id})
    except sqlite3.IntegrityError:
        # Duplicate – update embedding and other fields
        cur.execute('''
            UPDATE decisions
            SET file_patterns = ?,
                confidence = ?,
                event_type = ?,
                reinforcement_count = reinforcement_count + 1,
                last_seen_commit = ?,
                last_reinforced_at = datetime('now'),
                embedding = ?
            WHERE decision = ? AND module = ?
        ''', (
            decision_dict['file_patterns'],
            decision_dict['confidence'],
            decision_dict.get('event_type', 'NEW'),
            decision_dict.get('last_seen_commit'),
            embedding_bytes,
            decision_dict['decision'],
            decision_dict['module']
        ))
        conn.commit()
        cur.execute('SELECT id FROM decisions WHERE decision = ? AND module = ?',
                    (decision_dict['decision'], decision_dict['module']))
        decision_id = cur.fetchone()[0]
        logger.info("Decision updated with embedding (duplicate)", extra={'decision_id': decision_id})
    conn.close()
    return decision_id

def _match_any_file(file_patterns, file_list_str):
    patterns = [p.strip() for p in file_patterns.split(',')]
    files = file_list_str.split('|')
    for f in files:
        for p in patterns:
            if fnmatch.fnmatch(f, p):
                return 1
    return 0

def get_decisions_for_files(db_path, file_list, top_k=5):
    logger.debug("Getting decisions for files", extra={'file_count': len(file_list), 'top_k': top_k})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.create_function("match_files", 2, _match_any_file)
    
    file_list_str = '|'.join(file_list)
    
    cur = conn.cursor()
    cur.execute('''
        SELECT * FROM decisions 
        WHERE flagged = 0 AND archived = 0 
        AND match_files(file_patterns, ?) = 1
        ORDER BY (reinforcement_count * CASE confidence WHEN 'HIGH' THEN 3 WHEN 'MEDIUM' THEN 2 ELSE 1 END) DESC, last_seen_commit DESC
        LIMIT ?
    ''', (file_list_str, top_k))
    
    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    logger.debug("Retrieved decisions", extra={'count': len(results)})
    return results

def get_relevant_decisions(db_path, file_list, task_description=None, top_k=10):
    """Return decisions relevant to the given files and optional task description.
    Uses file pattern matching plus a placeholder for semantic similarity.
    Currently returns same as get_decisions_for_files but with top_k default 10."""
    logger.debug("Getting relevant decisions", extra={'file_count': len(file_list), 'top_k': top_k})
    return get_decisions_for_files(db_path, file_list, top_k=top_k)

def reinforce_decision(db_path, decision_id, commit_hash):
    logger.info("Reinforcing decision", extra={'decision_id': decision_id, 'commit': commit_hash[:8]})
    conn = sqlite3.connect(db_path)
    conn.execute('''
        UPDATE decisions 
        SET reinforcement_count = reinforcement_count + 1, 
            last_seen_commit = ?,
            last_reinforced_at = datetime('now')
        WHERE id = ?
    ''', (commit_hash, decision_id))
    conn.commit()
    conn.close()

def flag_decision(db_path, decision_id):
    logger.info("Flagging decision", extra={'decision_id': decision_id})
    conn = sqlite3.connect(db_path)
    conn.execute('UPDATE decisions SET flagged = 1 WHERE id = ?', (decision_id,))
    conn.commit()
    conn.close()

def archive_decision(db_path, decision_id):
    logger.info("Archiving decision", extra={'decision_id': decision_id})
    conn = sqlite3.connect(db_path)
    conn.execute('UPDATE decisions SET archived = 1 WHERE id = ?', (decision_id,))
    conn.commit()
    conn.close()

def get_flagged_decisions(db_path):
    logger.debug("Getting flagged decisions")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM decisions WHERE flagged = 1 AND archived = 0")
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    logger.debug("Retrieved flagged decisions", extra={'count': len(res)})
    return res

def get_active_decisions_by_module(db_path, module):
    # 'module' is reserved on logging.LogRecord (it is the source module name),
    # so passing extra={'module': ...} raises KeyError and killed `kl review`
    # before it printed a single conflict.
    logger.debug("Getting active decisions by module", extra={'decision_module': module})
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM decisions WHERE module = ? AND flagged = 0 AND archived = 0 ORDER BY reinforcement_count DESC", (module,))
    res = [dict(r) for r in cur.fetchall()]
    conn.close()
    return res

def resolve_decision(db_path, decision_id, action, old_id=None, new_text=None):
    logger.info("Resolving decision", extra={'decision_id': decision_id, 'action': action})
    conn = sqlite3.connect(db_path)
    if action == 'accept':
        if old_id:
            conn.execute('UPDATE decisions SET archived = 1 WHERE id = ?', (old_id,))
        conn.execute("UPDATE decisions SET flagged = 0, confidence = 'MEDIUM' WHERE id = ?", (decision_id,))
    elif action == 'reject':
        conn.execute('UPDATE decisions SET archived = 1 WHERE id = ?', (decision_id,))
    elif action == 'edit':
        if old_id:
            conn.execute('UPDATE decisions SET archived = 1 WHERE id = ?', (old_id,))
        conn.execute("UPDATE decisions SET flagged = 0, confidence = 'MEDIUM', decision = ? WHERE id = ?", (new_text, decision_id))
    conn.commit()
    conn.close()

def get_existing_decisions_for_files(file_paths: list[str]) -> list[dict]:
    db_path = str(Path('.klyd/memory.db').resolve())
    if not Path(db_path).exists():
        return []
    decisions = get_decisions_for_files(db_path, file_paths, top_k=50)
    high_conf = [d for d in decisions if d['confidence'] == 'HIGH']
    return [{'id': d['id'], 'decision': d['decision']} for d in high_conf[:5]]

# ----------------------------------------------------------------------
# Conflict resolution helpers
# ----------------------------------------------------------------------

def get_decision_by_id(db_path: str, decision_id: int) -> dict | None:
    """Retrieve a single decision by its id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None

def create_decision_version(db_path: str, original_id: int, new_decision_dict: dict) -> int:
    """
    Create a new version of a decision, linking it to the original via parent_decision_id.
    The new decision gets version_id = original.version_id + 1.
    Returns the id of the new decision.
    """
    original = get_decision_by_id(db_path, original_id)
    if not original:
        raise ValueError(f"Decision with id {original_id} not found")
    
    new_version = original['version_id'] + 1
    new_dict = {
        'decision': new_decision_dict.get('decision', original['decision']),
        'module': new_decision_dict.get('module', original['module']),
        'file_patterns': new_decision_dict.get('file_patterns', original['file_patterns']),
        'confidence': new_decision_dict.get('confidence', original['confidence']),
        'event_type': new_decision_dict.get('event_type', 'NEW'),
        'last_seen_commit': new_decision_dict.get('last_seen_commit', original.get('last_seen_commit')),
        'embedding': new_decision_dict.get('embedding', original.get('embedding')),
    }
    
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO decisions
            (decision, module, file_patterns, confidence, event_type,
             last_seen_commit, embedding, version_id, parent_decision_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        new_dict['decision'],
        new_dict['module'],
        new_dict['file_patterns'],
        new_dict['confidence'],
        new_dict['event_type'],
        new_dict.get('last_seen_commit'),
        new_dict.get('embedding'),
        new_version,
        original_id
    ))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    logger.info("Created decision version", extra={'original_id': original_id, 'new_id': new_id, 'version': new_version})
    return new_id

def merge_decisions(db_path: str, keep_id: int, archive_id: int):
    """
    Merge two decisions: archive the one with archive_id and set its merged_into_id to keep_id.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        UPDATE decisions
        SET archived = 1, merged_into_id = ?
        WHERE id = ?
    ''', (keep_id, archive_id))
    conn.commit()
    conn.close()
    logger.info("Merged decisions", extra={'keep_id': keep_id, 'archive_id': archive_id})

def auto_archive_old_decisions(db_path: str):
    """
    Archive decisions that have auto_archive_after set and are older than that many days.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        UPDATE decisions
        SET archived = 1
        WHERE archived = 0
          AND auto_archive_after IS NOT NULL
          AND datetime(created_at, '+' || auto_archive_after || ' days') < datetime('now')
    ''')
    conn.commit()
    conn.close()
    logger.info("Auto-archived old decisions")

def get_decision_versions(db_path: str, decision_id: int) -> list[dict]:
    """
    Return all versions of a decision, following the parent chain.
    Returns a list ordered by version_id ascending.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        WITH RECURSIVE ancestors(id, parent_decision_id, version_id, decision, module, file_patterns, confidence, event_type, reinforcement_count, last_seen_commit, created_at, flagged, archived, embedding, last_reinforced_at, relevance_score, merged_into_id, auto_archive_after) AS (
            SELECT id, parent_decision_id, version_id, decision, module, file_patterns, confidence, event_type, reinforcement_count, last_seen_commit, created_at, flagged, archived, embedding, last_reinforced_at, relevance_score, merged_into_id, auto_archive_after
            FROM decisions
            WHERE id = ?
            UNION ALL
            SELECT d.id, d.parent_decision_id, d.version_id, d.decision, d.module, d.file_patterns, d.confidence, d.event_type, d.reinforcement_count, d.last_seen_commit, d.created_at, d.flagged, d.archived, d.embedding, d.last_reinforced_at, d.relevance_score, d.merged_into_id, d.auto_archive_after
            FROM decisions d
            INNER JOIN ancestors a ON d.id = a.parent_decision_id
        )
        SELECT * FROM ancestors ORDER BY version_id ASC
    ''', (decision_id,))
    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    return results

def get_decision_ancestry(db_path: str, decision_id: int) -> list[dict]:
    """
    Return the chain of ancestors (parents) for a given decision, starting from the root.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        WITH RECURSIVE ancestors(id, parent_decision_id, version_id, decision, module, file_patterns, confidence, event_type, reinforcement_count, last_seen_commit, created_at, flagged, archived, embedding, last_reinforced_at, relevance_score, merged_into_id, auto_archive_after) AS (
            SELECT id, parent_decision_id, version_id, decision, module, file_patterns, confidence, event_type, reinforcement_count, last_seen_commit, created_at, flagged, archived, embedding, last_reinforced_at, relevance_score, merged_into_id, auto_archive_after
            FROM decisions
            WHERE id = ?
            UNION ALL
            SELECT d.id, d.parent_decision_id, d.version_id, d.decision, d.module, d.file_patterns, d.confidence, d.event_type, d.reinforcement_count, d.last_seen_commit, d.created_at, d.flagged, d.archived, d.embedding, d.last_reinforced_at, d.relevance_score, d.merged_into_id, d.auto_archive_after
            FROM decisions d
            INNER JOIN ancestors a ON d.id = a.parent_decision_id
        )
        SELECT * FROM ancestors ORDER BY version_id ASC
    ''', (decision_id,))
    results = [dict(row) for row in cur.fetchall()]
    conn.close()
    return results

# ----------------------------------------------------------------------
# Metrics helpers
# ----------------------------------------------------------------------

def record_metric(db_path: str, metric_name: str, metric_value: float):
    """Record a metric value in the metrics table."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO metrics (metric_name, metric_value)
        VALUES (?, ?)
    ''', (metric_name, metric_value))
    conn.commit()
    conn.close()
    logger.debug("Recorded metric", extra={'metric_name': metric_name, 'metric_value': metric_value})

def get_metrics_summary(db_path: str) -> dict:
    """Return a summary of the latest metrics."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Get latest decision_count
    cur.execute('''
        SELECT metric_value FROM metrics
        WHERE metric_name = 'decision_count'
        ORDER BY timestamp DESC LIMIT 1
    ''')
    row = cur.fetchone()
    decision_count = row[0] if row else 0

    # Get latest avg_confidence
    cur.execute('''
        SELECT metric_value FROM metrics
        WHERE metric_name = 'avg_confidence'
        ORDER BY timestamp DESC LIMIT 1
    ''')
    row = cur.fetchone()
    avg_confidence = row[0] if row else 0.0

    # Get latest conflict_rate
    cur.execute('''
        SELECT metric_value FROM metrics
        WHERE metric_name = 'conflict_rate'
        ORDER BY timestamp DESC LIMIT 1
    ''')
    row = cur.fetchone()
    conflict_rate = row[0] if row else 0.0

    conn.close()
    return {
        'decision_count': decision_count,
        'avg_confidence': avg_confidence,
        'conflict_rate': conflict_rate
    }

def update_metrics(db_path: str):
    """Compute and record current metrics from the decisions table."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Total active decisions
    cur.execute("SELECT COUNT(*) FROM decisions WHERE archived = 0")
    total = cur.fetchone()[0]
    # Average confidence (map to numeric)
    cur.execute("SELECT confidence FROM decisions WHERE archived = 0")
    confs = [row[0] for row in cur.fetchall()]
    conf_map = {'LOW': 0, 'MEDIUM': 1, 'HIGH': 2}
    avg_conf = sum(conf_map.get(c, 0) for c in confs) / len(confs) if confs else 0.0
    # Conflict rate
    cur.execute("SELECT COUNT(*) FROM decisions WHERE archived = 0 AND flagged = 1")
    flagged = cur.fetchone()[0]
    conflict_rate = flagged / total if total > 0 else 0.0
    conn.close()

    record_metric(db_path, 'decision_count', total)
    record_metric(db_path, 'avg_confidence', avg_conf)
    record_metric(db_path, 'conflict_rate', conflict_rate)
    logger.info("Metrics updated", extra={'decision_count': total, 'avg_confidence': avg_conf, 'conflict_rate': conflict_rate})
