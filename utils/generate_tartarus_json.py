#!/usr/bin/env python3
"""Build small, selectable Tartarus datasets from the bundled source decks."""

import argparse
import glob
import json
import os
from collections import Counter


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DIR = os.path.join(ROOT, 'data', 'sources')
OUTPUT_DIR = os.path.join(ROOT, 'data', 'word_lists')
LEVELS = ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')

GENDER_ARTICLE = {'masculine': 'der', 'feminine': 'die', 'neuter': 'das'}
POS_NORMALIZATION = {
    'adj': 'adjective', 'adjektiv': 'adjective',
    'adv': 'adverb',
    'n': 'noun',
    'num': 'numeral', 'number': 'numeral',
    'v': 'verb', 'v1': 'verb',
    'none': 'other', 'unclear': 'other', 'discard': 'other',
    '[keep as-is]': 'other', '[as-is]': 'other', '[pos_edited]': 'other',
}
POS_CATEGORIES = {'noun', 'verb', 'adjective', 'adverb', 'pronoun', 'numeral', 'conjunction', 'interjection'}
POS_FILE_NAMES = {
    'verb': 'verbs', 'adjective': 'adjectives', 'adverb': 'adverbs',
    'pronoun': 'pronouns', 'numeral': 'numerals',
    'conjunction': 'conjunctions', 'interjection': 'interjections',
    'other': 'other',
}
NOUN_GENDER_GROUPS = {
    'masculine': 'nouns_masculine',
    'feminine': 'nouns_feminine',
    'neuter': 'nouns_neuter',
    'variable': 'nouns_variable',
    'plural_only': 'nouns_plural',
}


def normalize_pos(value):
    raw = str(value or '').strip().lower()
    return POS_NORMALIZATION.get(raw, raw if raw in POS_CATEGORIES else 'other')


def category(record, language):
    pos = normalize_pos(record.get('pos'))
    if pos == 'noun':
        if language != 'german':
            return 'nouns'
        gender = str(record.get('gender') or '').strip().lower()
        if gender in GENDER_ARTICLE:
            return f'nouns_{gender}'
        if gender in NOUN_GENDER_GROUPS:
            return NOUN_GENDER_GROUPS[gender]
        return 'nouns_unknown'
    return POS_FILE_NAMES[pos]


def frequency(record):
    value = record.get('word_frequency')
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def definition_lines(record, language):
    translation = str(record.get('english_translation') or '').strip()
    native = str(record.get('example_sentence_native') or '').strip()
    english = str(record.get('example_sentence_english') or '').strip()
    lines = [translation] if translation else []
    if native and english and language == 'german':
        lines.append(f'{native} — {english}')
    elif english:
        lines.append(english)
    return lines


def vocab_entry(record, language):
    word = str(record.get('word') or '').strip()
    if not word:
        return None
    if language == 'german' and normalize_pos(record.get('pos')) == 'noun':
        article = GENDER_ARTICLE.get(str(record.get('gender') or '').strip().lower())
        if article:
            word = f'{article} {word}'
    lines = definition_lines(record, language)
    if not lines:
        return None
    entry = {'word': word, 'definition': lines if len(lines) > 1 else lines[0]}
    freq = frequency(record)
    if freq is not None:
        entry['word_frequency'] = freq
    return entry


def sentence_entry(record):
    native = str(record.get('example_sentence_native') or '').strip()
    english = str(record.get('example_sentence_english') or '').strip()
    if not native or not english:
        return None
    entry = {'word': native, 'definition': english}
    freq = frequency(record)
    if freq is not None:
        entry['word_frequency'] = freq
    return entry


def merge_entry(existing, candidate):
    """Merge exact duplicate identities without losing useful definitions."""
    old_defs = existing['definition'] if isinstance(existing['definition'], list) else [existing['definition']]
    new_defs = candidate['definition'] if isinstance(candidate['definition'], list) else [candidate['definition']]
    definitions = list(dict.fromkeys([value for value in old_defs + new_defs if value]))
    existing['definition'] = definitions if len(definitions) > 1 else definitions[0]
    old_freq = existing.get('word_frequency')
    new_freq = candidate.get('word_frequency')
    if old_freq is None or (new_freq is not None and new_freq < old_freq):
        if new_freq is not None:
            existing['word_frequency'] = new_freq
    return existing


def identity(record, language):
    return (
        str(record.get('word') or '').strip().casefold(),
        normalize_pos(record.get('pos')),
        str(record.get('gender') or '').strip().lower(),
        str(record.get('cefr_level') or '').strip().upper(),
    )


def load_records(language):
    path = os.path.join(SOURCE_DIR, f'{language}.json')
    with open(path, encoding='utf-8') as source:
        return json.load(source)


def clean_generated_outputs(language):
    for path in glob.glob(os.path.join(OUTPUT_DIR, f'{language}_*.json')):
        if os.path.basename(path).startswith(f'{language}_goethe_'):
            continue
        os.remove(path)


def generate(language):
    clean_generated_outputs(language)
    records = load_records(language)
    vocab = {}
    sentences = {}
    skipped = Counter()
    for record in records:
        level = str(record.get('cefr_level') or '').strip().upper()
        if level not in LEVELS:
            skipped['invalid_cefr'] += 1
            continue
        group = category(record, language)
        key = identity(record, language)
        vocab_entry_value = vocab_entry(record, language)
        if vocab_entry_value is None:
            skipped['missing_vocab_data'] += 1
        else:
            vocab.setdefault((group, level, key), vocab_entry_value)
            if vocab[(group, level, key)] is not vocab_entry_value:
                merge_entry(vocab[(group, level, key)], vocab_entry_value)
        sentence_entry_value = sentence_entry(record)
        if sentence_entry_value is not None:
            sentence_key = (group, level, sentence_entry_value['word'].casefold())
            sentences.setdefault(sentence_key, sentence_entry_value)
            if sentences[sentence_key] is not sentence_entry_value:
                merge_entry(sentences[sentence_key], sentence_entry_value)
        else:
            skipped['missing_sentence_data'] += 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    generated = Counter()
    for (group, level, _), entry in sorted(vocab.items()):
        path = os.path.join(OUTPUT_DIR, f'{language}_{group}_{level.lower()}.json')
        generated[path] += 1
    for path in generated:
        entries = [entry for (group, level, _), entry in sorted(vocab.items())
                   if path.endswith(f'{language}_{group}_{level.lower()}.json')]
        with open(path, 'w', encoding='utf-8') as output:
            json.dump(entries, output, ensure_ascii=False, indent=2)

    sentence_generated = Counter()
    for (group, level, _), entry in sorted(sentences.items()):
        path = os.path.join(OUTPUT_DIR, f'{language}_{group}_sentences_{level.lower()}.json')
        sentence_generated[path] += 1
    for path in sentence_generated:
        entries = [entry for (group, level, _), entry in sorted(sentences.items())
                   if path.endswith(f'{language}_{group}_sentences_{level.lower()}.json')]
        with open(path, 'w', encoding='utf-8') as output:
            json.dump(entries, output, ensure_ascii=False, indent=2)

    print(f'{language}: {len(records)} source records, {len(vocab)} vocabulary records, '
          f'{len(sentences)} sentence records')
    print(f'  vocabulary files: {len(generated)}; sentence files: {len(sentence_generated)}')
    if skipped:
        print(f'  skipped/flagged: {dict(skipped)}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lang', choices=('german', 'english'), default='german')
    parser.add_argument('--all', action='store_true', help='Generate both German and English datasets.')
    args = parser.parse_args()
    for language in ('german', 'english') if args.all else (args.lang,):
        generate(language)


if __name__ == '__main__':
    main()
