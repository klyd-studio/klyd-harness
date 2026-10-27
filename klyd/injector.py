import fnmatch
import math
import struct
from .db import (
    compute_embedding,
    get_embedding_for_decision,
    get_reinforcement_recency_score,
)
from .config import get_injection_template, get_pinned_decision_ids, get_max_decisions_inject, get_strict_mode, get_config
from .logger import setup_logger

logger = setup_logger(__name__)


def _cosine_similarity(a: bytes, b: bytes) -> float:
    """Cosine similarity between two float32 little-endian vectors.

    The placeholder embedding is 128 zero bytes, so a zero norm is the normal
    case today, not an edge case: return 0.0 instead of dividing by zero.
    """
    if not a or not b or len(a) != len(b) or len(a) % 4 != 0:
        return 0.0
    va = struct.unpack(f"<{len(a) // 4}f", a)
    vb = struct.unpack(f"<{len(b) // 4}f", b)
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)

def _file_pattern_match(file_patterns: str, file_list: list[str]) -> float:
    """Return 1.0 if any file matches any pattern, else 0.0."""
    patterns = [p.strip() for p in file_patterns.split(',')]
    for f in file_list:
        for p in patterns:
            if fnmatch.fnmatch(f, p):
                return 1.0
    return 0.0

def _compute_semantic_similarity(db_path: str, decision_id: int, task_embedding: bytes) -> float:
    """Compute cosine similarity between task embedding and decision embedding."""
    decision_emb = get_embedding_for_decision(db_path, decision_id)
    if not decision_emb or not task_embedding:
        return 0.0
    return _cosine_similarity(task_embedding, decision_emb)

def _compute_recency_score(db_path: str, decision_id: int) -> float:
    """Return recency score between 0 and 1."""
    return get_reinforcement_recency_score(db_path, decision_id)

def format_injection(
    decisions,
    db_path=None,
    task_description=None,
    relevance_mode='balanced',
    top_k=None,
    template=None,
    min_confidence=None,
    module_filter=None,
    pinned_ids=None,
    preview=False
):
    """
    Format decisions for injection, optionally scoring by relevance.
    
    Parameters:
        decisions: list of decision dicts (as returned by get_decisions_for_files)
        db_path: path to the memory.db (needed for embedding retrieval)
        task_description: optional string describing the current task
        relevance_mode: 'balanced' (default) or 'strict'
        top_k: maximum number of decisions to include (overrides config if provided)
        template: custom template string with {decisions} placeholder (overrides config)
        min_confidence: minimum confidence level to include ('LOW', 'MEDIUM', 'HIGH')
        module_filter: list of module names to include (if provided, only decisions with module in this list)
        pinned_ids: list of decision ids to always include (overrides config if provided)
        preview: if True, print the formatted output to console instead of returning
    Returns:
        formatted string (if preview=False)
    """
    logger.debug("Formatting injection", extra={
        'decision_count': len(decisions),
        'relevance_mode': relevance_mode,
        'top_k': top_k,
        'preview': preview
    })
    if not decisions:
        return ""

    # Use config values if not overridden
    if top_k is None:
        top_k = get_max_decisions_inject()
    if template is None:
        template = get_injection_template()
    if pinned_ids is None:
        pinned_ids = get_pinned_decision_ids()
    if min_confidence is None:
        min_confidence = get_config('min_confidence', 'LOW')
    if module_filter is None:
        module_filter = get_config('module_filter', [])
    
    strict_mode = get_strict_mode()

    # If strict_mode is True, override relevance_mode to 'strict'
    if strict_mode:
        relevance_mode = 'strict'

    # Filter by confidence
    confidence_order = {'LOW': 0, 'MEDIUM': 1, 'HIGH': 2}
    min_conf_val = confidence_order.get(min_confidence.upper(), 0)
    decisions = [d for d in decisions if confidence_order.get(d['confidence'], 0) >= min_conf_val]

    # Filter by module
    if module_filter:
        decisions = [d for d in decisions if d['module'] in module_filter]

    # Separate pinned decisions from the rest
    pinned = [d for d in decisions if d['id'] in pinned_ids]
    unpinned = [d for d in decisions if d['id'] not in pinned_ids]

    # If task_description is provided, compute its embedding once
    task_embedding = None
    if task_description and db_path:
        task_embedding = compute_embedding(task_description)

    # Compute scores for unpinned decisions
    scored = []
    for d in unpinned:
        file_match = 1.0  # decisions already filtered by file patterns

        semantic_sim = 0.0
        if task_embedding and db_path:
            semantic_sim = _compute_semantic_similarity(db_path, d['id'], task_embedding)

        recency = 0.0
        if db_path:
            recency = _compute_recency_score(db_path, d['id'])

        # Weighted score
        score = file_match * 0.4 + semantic_sim * 0.4 + recency * 0.2

        # In strict mode, only include decisions with file_match=1 (already true)
        if relevance_mode == 'strict' and file_match < 1.0:
            continue

        scored.append((score, d))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Take top_k from unpinned (after pinned)
    remaining_slots = max(0, top_k - len(pinned))
    top_unpinned = [d for _, d in scored[:remaining_slots]]

    # Combine pinned (always included) with top unpinned
    final_decisions = pinned + top_unpinned

    if not final_decisions:
        return ""

    # Build the decision lines
    decision_lines = []
    for i, d in enumerate(final_decisions, 1):
        mod = f"[{d['module']}]"
        conf = f"{d['confidence']} confidence"
        count = f"confirmed {d['reinforcement_count']} times"
        dec = d['decision'].rstrip('.')  # remove trailing period if present
        decision_lines.append(f"{i}. {mod} {dec}. ({conf}, {count})")

    # Format using the template
    decisions_text = "\n".join(decision_lines)
    result = template.replace("{decisions}", decisions_text)

    if preview:
        from rich.console import Console
        console = Console()
        console.print(result)
        return result

    logger.info("Injection formatted", extra={'decision_count': len(final_decisions)})
    return result
