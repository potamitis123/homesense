"""Query expansion table for CLAP search.

The categories live in common_files/clap_query_expansions.json and are the
single source of truth. Each category lists trigger keywords; when a search
query contains one, that category's related/negative terms are folded into
the CLAP search (ensemble + contrastive scoring) via clap_service.search().

Re-read whenever the file's timestamp changes, so editing the table takes
effect on the next search without restarting anything.
"""

import json
import os
import threading

_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'common_files', 'clap_query_expansions.json')

_lock = threading.Lock()
_cache = []
_mtime = None


def _load_locked():
    global _cache, _mtime
    try:
        stamp = os.path.getmtime(_PATH)
    except OSError:
        _cache, _mtime = [], None
        return
    if stamp == _mtime:
        return
    try:
        with open(_PATH, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # Keep serving the last good table: a half-saved file should not
        # silently strip expansion from every search.
        print(f"[clap_expansions] cannot read {_PATH}: {exc}")
        _mtime = stamp
        return
    categories = []
    for cat in (data or {}).get('categories', []):
        triggers = [str(t).lower() for t in cat.get('triggers', []) if str(t).strip()]
        if not triggers:
            continue
        categories.append({
            'triggers': triggers,
            'related': [str(r) for r in cat.get('related', []) if str(r).strip()],
            'negative': [str(n) for n in cat.get('negative', []) if str(n).strip()],
        })
    _cache = categories
    _mtime = stamp


def expand_query(query_text):
    """Related and negative terms for a search query, from matching categories.

    Matching is a case-insensitive substring test against each category's
    trigger keywords. A query can match more than one category; their terms
    are unioned (order-preserving, deduplicated).

    Returns (related_queries, negative_queries) -- both empty lists if
    nothing matched, so callers can fall back to a plain, unexpanded search.
    """
    if not query_text:
        return [], []
    clean = query_text.lower()
    with _lock:
        _load_locked()
        categories = _cache

    related, negative = [], []
    for cat in categories:
        if any(trigger in clean for trigger in cat['triggers']):
            for term in cat['related']:
                if term not in related:
                    related.append(term)
            for term in cat['negative']:
                if term not in negative:
                    negative.append(term)
    return related, negative
