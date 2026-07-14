#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate Tartarus word-list JSON files from the bundled source decks.

Source: https://github.com/vbvss199/Language-Learning-decks

Supported source files (place in data/word_lists/):
  german.json, english.json, hiragana.json, kanji.json, katakana.json

Usage
-----
  # Vocabulary mode (one file per CEFR level)
  python3 utils/generate_tartarus_json.py --lang german  --user bahman
  python3 utils/generate_tartarus_json.py --lang english --user bahman
  python3 utils/generate_tartarus_json.py --lang kanji   --user tartarus

  # Sentence mode: word = native sentence, definition = English sentence
  python3 utils/generate_tartarus_json.py --lang german --user bahman --sentences
  python3 utils/generate_tartarus_json.py --lang kanji  --user tartarus  --sentences

  # Single CEFR level
  python3 utils/generate_tartarus_json.py --lang german --user bahman --cefr B1

  # Flashcard-quality entries only (useful_for_flashcard = true)
  python3 utils/generate_tartarus_json.py --lang kanji --user tartarus --flashcard-only

Output
------
  Vocabulary : data/word_lists/<user>_<lang>_<level>.json
  Sentences  : data/word_lists/<user>_<lang>_sentences_<level>.json

Vocabulary definition format
----------------------------
  German : line 1 = english_translation
           line 2 = "native sentence — english sentence"
           word   = "der/die/das Word" for nouns, bare word otherwise

  Japanese : line 1 = "romanization — english_translation"
             line 2 = "native sentence — english sentence" (when available)
             word   = Japanese word as-is (hiragana/katakana/kanji)

  English  : line 1 = english_translation (= definition for English deck)
             line 2 = english example sentence (when available)
             word   = English word as-is
"""
import os
import sys
import json
import argparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORD_LISTS_DIR = os.path.join(_ROOT, 'data', 'word_lists')

GENDER_ARTICLE = {
    'masculine': 'der',
    'feminine':  'die',
    'neuter':    'das',
}

_POS_NORM = {
    'adj': 'adjective', 'adjektiv': 'adjective',
    'adv': 'adverb',
    'num': 'numeral', 'number': 'numeral',
    'v': 'verb', 'v1': 'verb',
    'n': 'noun',
    'none': '', 'unclear': '', 'discard': '',
    '[keep as-is]': '', '[as-is]': '', '[pos_edited]': '',
}

VALID_CEFR = {'A1', 'A2', 'B1', 'B2', 'C1', 'C2'}

JAPANESE_LANGS = {'hiragana', 'kanji', 'katakana'}


def normalize_pos(raw):
    return _POS_NORM.get(raw, raw).lower().strip()


def build_vocab_entry(record, lang):
    word = record.get('word', '').strip()
    if not word:
        return None

    translation = record.get('english_translation', '').strip()
    pos = normalize_pos(record.get('pos', ''))
    gender = record.get('gender', '')
    romanization = record.get('romanization', '').strip()
    native_sent = record.get('example_sentence_native', '').strip()
    english_sent = record.get('example_sentence_english', '').strip()

    if lang == 'german' and pos == 'noun':
        article = GENDER_ARTICLE.get(gender, '')
        word_field = f'{article} {word}' if article else word
    else:
        word_field = word

    definition = []
    if lang in JAPANESE_LANGS:
        # Line 1: romanization + translation together so the pronunciation
        # is always visible as the primary hint
        if romanization and translation:
            definition.append(f'{romanization} — {translation}')
        elif translation:
            definition.append(translation)
        elif romanization:
            definition.append(romanization)
    else:
        if translation:
            definition.append(translation)

    if native_sent and english_sent:
        definition.append(f'{native_sent} — {english_sent}')
    elif english_sent and lang not in JAPANESE_LANGS and lang != 'german':
        definition.append(english_sent)

    if not definition:
        return None

    return {
        'word': word_field,
        'definition': definition if len(definition) > 1 else definition[0],
    }


def build_sentence_entry(record):
    native = record.get('example_sentence_native', '').strip()
    english = record.get('example_sentence_english', '').strip()
    if not native or not english:
        return None
    return {'word': native, 'definition': english}


def generate(lang, user, cefr_filter=None, sentences=False, flashcard_only=False):
    source_path = os.path.join(WORD_LISTS_DIR, f'{lang}.json')
    if not os.path.exists(source_path):
        print(f'Source file not found: {source_path}', file=sys.stderr)
        sys.exit(1)

    with open(source_path, encoding='utf-8') as f:
        records = json.load(f)

    buckets = {}
    skipped = 0
    for record in records:
        if flashcard_only and not record.get('useful_for_flashcard'):
            skipped += 1
            continue
        level = record.get('cefr_level', '').strip().upper()
        if level not in VALID_CEFR:
            skipped += 1
            continue
        if cefr_filter and level != cefr_filter.upper():
            continue
        entry = build_sentence_entry(record) if sentences else build_vocab_entry(record, lang)
        if entry is None:
            skipped += 1
            continue
        buckets.setdefault(level, []).append(entry)

    if not buckets:
        print('No entries matched the given filters.')
        return

    os.makedirs(WORD_LISTS_DIR, exist_ok=True)
    for level in sorted(buckets):
        entries = buckets[level]
        kind = 'sentences_' if sentences else ''
        # Output generic project files by default (no user prefix unless provided)
        prefix = f'{user}_' if user else ''
        out_name = f'{prefix}{lang}_{kind}{level.lower()}.json'
        out_path = os.path.join(WORD_LISTS_DIR, out_name)
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        print(f'  {level}: {len(entries):>5} entries  →  {out_name}')

    if skipped:
        print(f'  (skipped {skipped} entries with missing/invalid CEFR or data)')

    suffix = f'{lang}_sentences_a1' if sentences else f'{lang}_a1'
    audio_hint = ' --audio-lang japanese' if lang in JAPANESE_LANGS else ''
    user_hint = f' --user <your_user>' if not user else f' --user {user}'
    print(f'\nDone. Run: ./tartarus.sh practice{user_hint} --lang {suffix}{audio_hint}')


def main():
    valid_langs = ['german', 'english', 'hiragana', 'kanji', 'katakana']
    parser = argparse.ArgumentParser(
        description='Generate Tartarus JSON word lists from the bundled source decks.'
    )
    parser.add_argument('--lang', required=True, choices=valid_langs,
                        help='Source deck language.')
    parser.add_argument('--user', default='',
                        help='Username prefix for output filenames (optional, for user-specific lists).')
    parser.add_argument('--cefr', metavar='LEVEL',
                        help='Only generate one CEFR level (A1/A2/B1/B2/C1/C2).')
    parser.add_argument('--sentences', action='store_true',
                        help='Sentence mode: word = native sentence, definition = English sentence.')
    parser.add_argument('--flashcard-only', action='store_true',
                        help='Only include entries marked useful_for_flashcard=true.')
    args = parser.parse_args()

    mode = 'sentence' if args.sentences else 'vocabulary'
    user_desc = f' for user "{args.user}"' if args.user else ' (generic project files)'
    print(f'Generating {args.lang.upper()} {mode} lists{user_desc}...')
    if args.cefr:
        print(f'Filtering to CEFR level: {args.cefr.upper()}')
    if args.flashcard_only:
        print('Flashcard-quality entries only.')
    generate(args.lang, args.user, args.cefr, args.sentences, args.flashcard_only)


if __name__ == '__main__':
    main()
