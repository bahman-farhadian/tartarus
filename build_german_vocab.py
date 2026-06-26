#!/usr/bin/env python3
"""
build_german_vocab.py

Builds LexiLoop word-list JSON files from either:

  1. A PLAIN TEXT FILE — one German word (or stem) per line, optionally
     with a CEFR level prefix:

        Abend
        abbiegen
        A1: Hund
        B1: abbiegen

     Produces a single <user>_<lang>.json file (or one per level if
     levels are given).

  2. A STEM CSV — the bundled dictionary_a1a2b1_onlystems.csv format:

        "","level","stem"
        "1","A1","abend"

     Produces separate <user>_german_<level>.json files (a1 / a2 / b1).

For each word/stem the script:
  • Tries a sequence of candidate full forms (capitalize, +en, +e, …)
  • Queries the English Wiktionary REST API for the English definition
  • Queries the German Wiktionary for the grammatical gender of nouns
    (so nouns come out as "der Abend", "die Katze", "das Auto")

Progress is checkpointed after every word — Ctrl+C is safe; re-run to
continue from where you left off.

Usage:
    python3 build_german_vocab.py <input_file> <user> [lang]

    <input_file>  .txt (one word/stem per line) or .csv (stem CSV format)
    <user>        LexiLoop username (e.g. bahman)
    [lang]        Word-list name for .txt input (default: german)

Examples:
    python3 build_german_vocab.py my_words.txt bahman
    python3 build_german_vocab.py my_words.txt bahman german_travel
    python3 build_german_vocab.py data/word_lists/dictionary_a1a2b1_onlystems.csv bahman
"""

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# -------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------
DELAY = 0.5          # seconds between requests (~2 req/s — well within limits)
BACKOFF_BASE = 10    # seconds to wait after first 429 response
BACKOFF_MAX = 60     # cap on wait time
MAX_RETRIES = 4

ARTICLE = {'m': 'der', 'f': 'die', 'n': 'das'}


# -------------------------------------------------------------------
# HTTP helpers
# -------------------------------------------------------------------
def http_get(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'LexiLoop-vocab-builder/1.0 (educational; github.com/LexiLoop)'},
    )
    wait = BACKOFF_BASE
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503):
                print(f'    rate limited ({e.code}), waiting {wait}s…', flush=True)
                time.sleep(wait)
                wait = min(wait * 2, BACKOFF_MAX)
                continue
            return None
        except Exception:
            return None
    return None


# -------------------------------------------------------------------
# Wiktionary lookups
# -------------------------------------------------------------------
_INFLECTION_RE = re.compile(
    r'^(plural|genitive|dative|accusative|nominative|inflected|'
    r'past tense|present tense|alternative form|archaic form)',
    re.I,
)

def en_wiktionary(word):
    """Return (part_of_speech, definition) for the German entry, or None."""
    url = ('https://en.wiktionary.org/api/rest_v1/page/definition/'
           + urllib.parse.quote(word))
    data = http_get(url)
    if not data:
        return None
    for entry in data.get('de', []):
        for defn in entry.get('definitions', []):
            text = re.sub(r'<[^>]+>', '', defn.get('definition', '')).strip()
            if not text or _INFLECTION_RE.match(text):
                continue
            return entry.get('partOfSpeech', 'unknown'), text
    return None


def de_wiktionary_genus(word):
    """Return 'der'/'die'/'das' from the German Wiktionary, or None."""
    url = (
        'https://de.wiktionary.org/w/api.php?action=parse&format=json'
        f'&page={urllib.parse.quote(word)}&prop=wikitext'
    )
    data = http_get(url)
    if not data:
        return None
    wikitext = data.get('parse', {}).get('wikitext', {}).get('*', '')
    for pattern in (r'\|Genus\s*=\s*([mfn])', r'\|Genus 1\s*=\s*([mfn])'):
        m = re.search(pattern, wikitext)
        if m:
            return ARTICLE.get(m.group(1))
    return None


# -------------------------------------------------------------------
# Stem → candidate full forms
# -------------------------------------------------------------------
def stem_candidates(stem):
    """Ordered list of candidate full-word forms to try for a given stem."""
    stem = stem.strip()
    if not stem:
        return []
    cap = stem[0].upper() + stem[1:]
    seen, result = set(), []
    for c in [cap, stem + 'en', cap + 'en', stem + 'e', cap + 'e', stem]:
        if c not in seen:
            result.append(c)
            seen.add(c)
    return result


# -------------------------------------------------------------------
# Resolve a single word/stem to (canonical_word, definition)
# -------------------------------------------------------------------
def resolve(stem):
    """Try candidate forms until one is found on Wiktionary.

    Returns (word, definition) — definition may be '' if not found.
    """
    word, defn, found_pos = stem, '', None

    for candidate in stem_candidates(stem):
        result = en_wiktionary(candidate)
        time.sleep(DELAY)
        if result:
            found_pos, defn = result
            word = candidate
            break

    # Fetch article for nouns
    if found_pos and 'noun' in found_pos.lower():
        article = de_wiktionary_genus(word)
        time.sleep(DELAY)
        if article:
            word = f'{article} {word}'

    return word, defn


# -------------------------------------------------------------------
# Checkpoint helpers
# -------------------------------------------------------------------
def load_checkpoint(path):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_checkpoint(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------
# Input parsers
# -------------------------------------------------------------------
def parse_txt(path):
    """Parse a plain text file. Each line: [LEVEL:] word

    Returns list of {'stem': ..., 'level': ...} dicts.
    Level defaults to 'default' if not specified.
    """
    rows = []
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            m = re.match(r'^([A-Cc][1-2])\s*:\s*(.+)$', line, re.I)
            if m:
                rows.append({'level': m.group(1).lower(), 'stem': m.group(2).strip()})
            else:
                rows.append({'level': 'default', 'stem': line})
    return rows


def parse_csv(path):
    """Parse the bundled stem CSV (columns: "", level, stem)."""
    with open(path, newline='', encoding='utf-8') as f:
        return [
            {'level': r['level'].strip().lower(), 'stem': r['stem'].strip()}
            for r in csv.DictReader(f)
            if r.get('stem', '').strip()
        ]


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    input_path = sys.argv[1]
    user = sys.argv[2]
    lang_override = sys.argv[3] if len(sys.argv) > 3 else 'german'

    out_dir = os.path.dirname(os.path.abspath(input_path))
    checkpoint_path = os.path.join(out_dir, '.build_german_vocab_progress.json')

    # Auto-detect input format
    if input_path.endswith('.csv'):
        rows = parse_csv(input_path)
        is_csv = True
    else:
        rows = parse_txt(input_path)
        is_csv = False

    total = len(rows)
    print(f'{total} words/stems  |  checkpoint: {checkpoint_path}')
    print('Ctrl+C is safe — progress is saved after every word.\n')

    progress = load_checkpoint(checkpoint_path)

    # Rebuild level→seen-canonical tracking from checkpoint
    seen_per_level = {}
    for stem, rec in progress.items():
        if rec.get('done') and not rec.get('dup') and rec.get('word'):
            lvl = rec.get('level', 'default')
            seen_per_level.setdefault(lvl, set()).add(rec['word'])

    try:
        for i, row in enumerate(rows, 1):
            level = row['level']
            stem = row['stem']
            if not stem:
                continue

            # Use stem+level as checkpoint key (same stem can appear at diff levels)
            ck_key = f'{level}:{stem}'

            if progress.get(ck_key, {}).get('done'):
                rec = progress[ck_key]
                print(f'[{i}/{total}] cached  {stem} → {rec["word"]}')
                continue

            word, defn = resolve(stem)

            # Deduplicate within the same level
            level_seen = seen_per_level.setdefault(level, set())
            is_dup = word in level_seen
            if not is_dup:
                level_seen.add(word)

            status = 'dup' if is_dup else ('✓' if defn else '?')
            print(f'[{i}/{total}] {status}  {stem} → {word}: {defn[:55]}')

            progress[ck_key] = {
                'word': word, 'definition': defn,
                'level': level, 'done': True, 'dup': is_dup,
            }
            save_checkpoint(checkpoint_path, progress)

    except KeyboardInterrupt:
        print('\nInterrupted — progress saved. Re-run to continue.')

    # -------------------------------------------------------------------
    # Assemble output JSON files
    # -------------------------------------------------------------------
    by_level = {}
    seen_words = {}

    for row in rows:
        stem = row['stem']
        level = row['level']
        ck_key = f'{level}:{stem}'
        rec = progress.get(ck_key)
        if not rec or not rec.get('done') or rec.get('dup'):
            continue
        word = rec['word']
        lvl_seen = seen_words.setdefault(level, set())
        if word in lvl_seen:
            continue
        lvl_seen.add(word)
        by_level.setdefault(level, []).append(
            {'word': word, 'definition': rec['definition']}
        )

    print()
    for level, words in sorted(by_level.items()):
        if is_csv:
            lang = f'german_{level}'
        else:
            lang = lang_override if level == 'default' else f'{lang_override}_{level}'
        out_path = os.path.join(out_dir, f'{user}_{lang}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(words, f, ensure_ascii=False, indent=2)
        found = sum(1 for w in words if w['definition'])
        print(f'{level.upper():8}  {len(words):4} words  ({found} with definition)  →  {out_path}')


if __name__ == '__main__':
    main()
