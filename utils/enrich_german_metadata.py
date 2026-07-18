#!/usr/bin/env python3
"""Use the local Gemma endpoint to repair unresolved German metadata."""

import argparse
import json
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data' / 'sources' / 'german.json'
VALID_POS = {'noun', 'verb', 'adjective', 'adverb', 'pronoun', 'numeral', 'conjunction', 'interjection', 'other'}
VALID_GENDER = {'masculine', 'feminine', 'neuter', 'plural_only', 'not_applicable', 'unknown'}
UNCERTAIN_POS = {'none', 'unclear', 'discard', '[keep as-is]', '[as-is]', '[pos_edited]'}


def unresolved(record):
    return (record.get('pos') == 'noun' and record.get('gender') in ('', 'Unknown')) or str(record.get('pos', '')).strip().lower() in UNCERTAIN_POS


def request_batch(endpoint, model, records, timeout):
    payload = {
        'model': model,
        'stream': False,
        'think': False,
        'format': 'json',
        'options': {'temperature': 0, 'num_predict': 4096},
        'messages': [
            {
                'role': 'system',
                'content': (
                    'Classify German vocabulary metadata. Return only valid JSON with this exact shape: '
                    '{"items":[{"word":"...","pos":"...","gender":"...",'
                    '"confidence":0.0,"rationale":"..."}]}. '
                    'Allowed pos values: noun, verb, adjective, adverb, pronoun, numeral, conjunction, '
                    'interjection, other. Allowed gender values: masculine, feminine, neuter, plural_only, '
                    'not_applicable, unknown. For nouns, classify grammatical gender, not the gender of a person. '
                    'For every noun, use masculine, feminine, neuter, plural_only, or unknown; never use '
                    'not_applicable for a noun. Use plural_only for nouns that have no normal singular form. '
                    'Borrowed countable nouns without an established German article should use neuter or unknown. '
                    'Use not_applicable for non-nouns. '
                    'Do not change the spelling. Keep rationales short and factual.'
                ),
            },
            {
                'role': 'user',
                'content': json.dumps([
                    {
                        'word': r['word'],
                        'english_translation': r.get('english_translation', ''),
                        'cefr_level': r.get('cefr_level', ''),
                        'current_pos': r.get('pos', ''),
                        'current_gender': r.get('gender', ''),
                    }
                    for r in records
                ], ensure_ascii=False),
            },
        ],
    }
    request = urllib.request.Request(
        endpoint.rstrip('/') + '/api/chat',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    content = body.get('message', {}).get('content', '')
    parsed = json.loads(content) if isinstance(content, str) else content
    items = parsed.get('items') if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        raise ValueError('Gemma response did not contain an items array')
    return items


def validate(items, expected):
    by_word = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('Gemma returned a non-object item')
        word = str(item.get('word', '')).strip()
        pos = str(item.get('pos', '')).strip().lower()
        gender = str(item.get('gender', '')).strip().lower()
        confidence = item.get('confidence')
        if word in by_word or word not in expected:
            raise ValueError(f'Unexpected or duplicate Gemma word: {word!r}')
        if pos not in VALID_POS or gender not in VALID_GENDER:
            raise ValueError(f'Invalid classification for {word!r}: {pos!r}, {gender!r}')
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f'Invalid confidence for {word!r}')
        if pos == 'noun' and gender == 'not_applicable':
            raise ValueError(f'Noun has not_applicable gender: {word!r}')
        if pos != 'noun' and gender not in {'not_applicable', 'unknown'}:
            raise ValueError(f'Non-noun has grammatical gender: {word!r}')
        by_word[word] = {
            'word': word,
            'pos': pos,
            'gender': gender,
            'confidence': float(confidence),
            'rationale': str(item.get('rationale', '')).strip(),
        }
    missing = set(expected) - set(by_word)
    if missing:
        raise ValueError(f'Gemma omitted {len(missing)} words: {sorted(missing)}')
    return by_word


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', default='http://192.168.8.5:11434')
    parser.add_argument('--model', default='gemma4:12b')
    parser.add_argument('--batch-size', type=int, default=20)
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--results', type=Path, default=ROOT / 'data' / 'sources' / 'german_gemma_metadata.json')
    parser.add_argument('--apply', action='store_true', help='Apply validated metadata to german.json.')
    args = parser.parse_args()

    rows = json.loads(SOURCE.read_text(encoding='utf-8'))
    targets = [row for row in rows if unresolved(row)]
    expected = {row['word'] for row in targets}
    existing = {}
    if args.results.exists():
        existing = {row['word']: row for row in json.loads(args.results.read_text(encoding='utf-8'))}
    pending = [row for row in targets if row['word'] not in existing]
    print(f'targets={len(targets)} cached={len(existing)} pending={len(pending)}')

    for start in range(0, len(pending), args.batch_size):
        batch = pending[start:start + args.batch_size]
        last_error = None
        for attempt in range(3):
            try:
                result = validate(request_batch(args.endpoint, args.model, batch, args.timeout), {r['word'] for r in batch})
                existing.update(result)
                break
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f'Gemma batch failed after retries: {last_error}')
        args.results.write_text(json.dumps(sorted(existing.values(), key=lambda r: r['word'].casefold()), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'completed={min(start + len(batch), len(pending))}/{len(pending)}')

    validated = validate(list(existing.values()), expected)
    if args.apply:
        backup = SOURCE.with_suffix(f'.backup-{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json')
        shutil.copy2(SOURCE, backup)
        for row in rows:
            result = validated.get(row['word'])
            if result:
                row['pos'] = result['pos']
                row['gender'] = result['gender'] if result['pos'] == 'noun' else ''
        SOURCE.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'updated={SOURCE} backup={backup}')
    else:
        print('dry run: use --apply to update the source dataset')


if __name__ == '__main__':
    main()
