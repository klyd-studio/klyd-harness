import json
import urllib.request
from anthropic import Anthropic
from .db import get_existing_decisions_for_files, compute_embedding
from .logger import setup_logger

logger = setup_logger(__name__)

def _call_openai_compatible(url, key, model, prompt):
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
        return res['choices'][0]['message']['content']

def extract_decisions(diff, commit_message, existing_decisions, config_data, model='claude-sonnet-4-6'):
    logger.info("Extracting decisions from commit", extra={'model': model})
    files = []
    for line in diff.split('\n'):
        if line.startswith('diff --git a/'):
            parts = line.split(' b/')
            if len(parts) == 2:
                files.append(parts[1].strip())
    
    invariants = get_existing_decisions_for_files(list(set(files)))
    invariants_text = "\n".join([f"- [{inv['id']}] {inv['decision']}" for inv in invariants]) if invariants else "None"

    prompt = f"""You are an architectural decision extractor for a software project.

You will receive a git diff, a commit message, and a list of previously recorded architectural decisions for the files touched in this diff.

Your job: extract the literal text of the architectural decisions that this commit clearly enacts.

Architectural decisions are: data store choice, auth strategy, API boundary contracts, module responsibility assignments, dependency/library choices, error handling patterns.

Rules:
- Only record if the diff explicitly introduces, changes, or contradicts a decision
- Do not guess. Do not infer. Only record what is clearly shown in the diff.
- If the commit is a style fix, test update, or minor refactor with no architectural significance: return []
- IMPORTANT: A NEW decision should be recorded when the commit introduces a different architectural choice than previously recorded, even if both are in the same project. For example: first commit uses Click, second commit adds SQLite - these are NEW decisions about different architectural choices (CLI framework, database). Only classify as REINFORCE if the commit explicitly confirms or repeats the SAME decision (e.g., adding another Click-based command).
- For each decision, classify as NEW (new architectural choice), REINFORCE (confirms SAME existing decision), or CONTRADICT (conflicts with an existing decision)
- Assign confidence: HIGH (unmistakable from diff), MEDIUM (clear but could have context), LOW (possible but uncertain)
- If this commit contradicts any of the listed invariants, set event_type to 'CONTRADICT' and add a note in the 'decision' field describing the violation. Also provide a "resolution_suggestion" field with a short sentence on how to resolve the conflict (e.g., "Use the new approach and archive the old one" or "Merge both approaches by ...").
- For each decision, also provide a short dense "semantic_summary" (one sentence) that captures the essence of the decision. This will be used for semantic similarity search.

Return ONLY valid JSON. No prose. No markdown. No explanation.
- The 'decision' field must contain the exact architectural rule or invariant written in the diff, quoted verbatim if possible. Do not summarise module responsibilities. For example, prefer 'Authentication uses JWT access tokens exclusively; no server-side sessions' over 'Authentication is handled by auth.py'.
- If event_type is CONTRADICT, include a "resolution_suggestion" field.
Schema: [{{"decision": str, "module": str, "file_patterns": str, "confidence": "LOW"|"MEDIUM"|"HIGH", "event": "NEW"|"REINFORCE"|"CONTRADICT", "semantic_summary": str, "resolution_suggestion": str (only if event is CONTRADICT)}}]
If no decisions: return []

EXISTING DECISIONS FOR TOUCHED FILES:
{existing_decisions}

COMMIT MESSAGE:
{commit_message}

DIFF:
{diff}

Existing architectural invariants (do not violate unless explicitly instructed):
{invariants_text}
"""
    try:
        # Determine provider
        is_anthropic_model = model.startswith('claude-') or model.startswith('anthropic/')

        api_base = config_data.get('api_base')
        if api_base:
            # Point the OpenAI-compatible client at any gateway -- a
            # self-hosted proxy, LiteLLM, Ollama, or a local stub in tests.
            # Without this the provider URLs are hardcoded, which makes the
            # extraction path impossible to exercise without a paid key.
            key = (
                config_data.get('openai_key')
                or config_data.get('openrouter_key')
                or config_data.get('api_key')
                or 'not-needed'
            )
            url = api_base.rstrip('/') + '/chat/completions'
            content = _call_openai_compatible(url, key, model, prompt)
            logger.debug("API base override used", extra={'api_base': api_base})
        # Direct Anthropic API if we have api_key and no other keys
        elif is_anthropic_model and 'api_key' in config_data and not any(k in config_data for k in ['openai_key', 'openrouter_key', 'gemini_key', 'groq_key']):
            client = Anthropic(api_key=config_data['api_key'])
            # Strip anthropic/ prefix if present
            actual_model = model.replace('anthropic/', '')
            response = client.messages.create(
                model=actual_model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}]
            )
            content = response.content[0].text.strip()
            logger.debug("Anthropic API call succeeded")
        # Use OpenRouter for Anthropic models if we have openrouter_key
        elif is_anthropic_model and 'openrouter_key' in config_data:
            url, key = "https://openrouter.ai/api/v1/chat/completions", config_data['openrouter_key']
            content = _call_openai_compatible(url, key, model, prompt)
            logger.debug("OpenRouter API call succeeded")
        else:
            # OpenAI compatible providers
            if model.startswith('gpt-') or model.startswith('o1') or model.startswith('o3'):
                if 'openai_key' not in config_data: raise ValueError("OpenAI API key missing.")
                url, key = "https://api.openai.com/v1/chat/completions", config_data['openai_key']
            elif model.startswith('gemini-') or model.startswith('gemma-'):
                if 'gemini_key' not in config_data: raise ValueError("Gemini API key missing.")
                url, key = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", config_data['gemini_key']
            elif '/' in model:
                if 'openrouter_key' not in config_data: raise ValueError("OpenRouter API key missing.")
                url, key = "https://openrouter.ai/api/v1/chat/completions", config_data['openrouter_key']
            elif config_data.get('groq_key'):
                url, key = "https://api.groq.com/openai/v1/chat/completions", config_data['groq_key']
            elif config_data.get('openai_key'):
                url, key = "https://api.openai.com/v1/chat/completions", config_data['openai_key']
            elif config_data.get('openrouter_key'):
                url, key = "https://openrouter.ai/api/v1/chat/completions", config_data['openrouter_key']
            else:
                raise ValueError(f"No valid API key configured for model: {model}")
                
            content = _call_openai_compatible(url, key, model, prompt)
            logger.debug("OpenAI-compatible API call succeeded")
            
        # Parse output as JSON
        content = content.strip()
        if content.startswith('```json'):
            content = content[7:]
        if content.endswith('```'):
            content = content[:-3]
            
        content = content.strip()
        
        result = json.loads(content)
        if not isinstance(result, list):
            logger.warning("LLM returned non-list result", extra={'result': result})
            return []
        
        normalized = []
        for r in result:
            event_val = r.get('event') or r.get('event_type') or 'NEW'
            summary = r.get('semantic_summary', r.get('decision', ''))
            # Compute embedding from the semantic summary
            emb_bytes = compute_embedding(summary)
            
            decision_text = r.get('decision', 'Unknown')
            # If CONTRADICT and resolution_suggestion is provided, append it to the decision text
            if event_val == 'CONTRADICT':
                resolution = r.get('resolution_suggestion')
                if resolution:
                    decision_text = f"{decision_text} [Resolution: {resolution}]"
            
            normalized.append({
                'decision': decision_text,
                'module': r.get('module', '/'),
                'file_patterns': r.get('file_patterns', '*'),
                'confidence': r.get('confidence', 'LOW'),
                'event_type': event_val,
                'embedding_bytes': emb_bytes
            })
        
        logger.info(f"Extracted {len(normalized)} decisions", extra={'count': len(normalized)})
        return normalized

    except Exception as e:
        # Put the reason in the message: the stderr handler only prints the
        # message, so `extra={'error': ...}` meant the user saw "Extraction
        # failed" with no cause at all.
        logger.error(f"Extraction failed: {e}", extra={'error': str(e)})
        raise
