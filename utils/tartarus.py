# -*- coding: utf-8 -*-
import os
import re
import sys
import json
import time
import random
import sqlite3
import argparse
import subprocess
from datetime import date, datetime, timedelta

# --- Configuration ---
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
DATABASE_FILE = os.path.join(DATA_DIR, 'tartarus.db')
WORD_LISTS_DIR = os.path.join(DATA_DIR, 'word_lists')
NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')


class Colors:
    YELLOW = '\033[93m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    CYAN = '\033[96m'
    BLUE = '\033[94m'  # Masculine (der)
    ENDC = '\033[0m'


def split_word_forms(word_text):
    """Splits a word entry on commas into its accepted forms (e.g. singular
    and plural), stripping surrounding whitespace from each form so that
    "a, b", "a,b" and "a , b" are all equivalent."""
    return [form.strip() for form in word_text.split(',') if form.strip()]


def answer_matches(answer, word_text, sentence_mode=False):
    """Checks a typed answer against every accepted form of a word,
    case-sensitively (comma-separated forms like "das Haus, die Häuser").
    Also accepts the full text with all forms typed out, e.g.
    "das Haus, die Häuser", however the commas/spacing are written.

    In sentence_mode, commas are part of the sentence and must NOT be treated
    as form separators — a simple case-sensitive full-string comparison is
    used instead."""
    if sentence_mode:
        return answer.strip() == word_text.strip()
    forms = [form.strip() for form in split_word_forms(word_text)]
    answer_forms = [form.strip() for form in split_word_forms(answer)]
    if len(answer_forms) == 1 and answer_forms[0] in forms:
        return True
    return sorted(answer_forms) == sorted(forms)


def mask_sentence(sentence, score):
    """Mask a sentence progressively based on score (5-9).
    
    Score 1-4: 100% visible (no masking)
    Score 5: 75% visible (25% masked)
    Score 6: 50% visible (50% masked)
    Score 7: 25% visible (75% masked) - "final" level per spec
    Score 8: 12.5% visible
    Score 9: 6.25% visible
    
    Non-letter characters (spaces, punctuation) are never masked.
    """
    if score < 5:
        return sentence
    # Each step from 5-9 masks an additional 25% of remaining visible letters
    # Score 5: mask 25% (75% visible)
    # Score 6: mask 50% (50% visible)  
    # Score 7: mask 75% (25% visible)
    # Score 8: mask 87.5% (12.5% visible)
    # Score 9: mask 93.75% (6.25% visible)
    steps = min(score - 4, 5)  # 1 to 5 steps
    visible_ratio = 0.75 ** steps  # 0.75, 0.5625, 0.421875, 0.3164, 0.2373
    
    # Find all letter positions (a-z, A-Z, and unicode letters)
    letter_indices = [i for i, ch in enumerate(sentence) if ch.isalpha()]
    if not letter_indices:
        return sentence
    
    # Calculate how many letters to keep visible
    num_visible = max(1, int(len(letter_indices) * visible_ratio))
    # Keep first and last few letters visible for context, plus some in middle
    visible_indices = set()
    
    # Always show first and last letter
    visible_indices.add(letter_indices[0])
    visible_indices.add(letter_indices[-1])
    
    # Distribute remaining visible letters evenly
    remaining_visible = num_visible - 2
    if remaining_visible > 0 and len(letter_indices) > 2:
        step = max(1, (len(letter_indices) - 2) // (remaining_visible + 1))
        for i in range(1, len(letter_indices) - 1, step):
            if len(visible_indices) < num_visible:
                visible_indices.add(letter_indices[i])
    
    # Build masked sentence
    result = []
    for i, ch in enumerate(sentence):
        if ch.isalpha():
            result.append(ch if i in visible_indices else '_')
        else:
            result.append(ch)
    return ''.join(result)


def get_gender_color(word_text):
    """Returns ANSI color for a word based on its German article:
    der (masculine) -> blue, die (feminine) -> red, das (neuter) -> green.
    Words without an article (verbs, adjectives, other languages) -> green."""
    text_lower = word_text.lower()
    if text_lower.startswith("der "):
        return Colors.BLUE
    if text_lower.startswith("die "):
        return Colors.RED
    if text_lower.startswith("das "):
        return Colors.GREEN
    return Colors.GREEN


# Maps common --lang names/codes to the locale prefix 'say' voices use
# (e.g. "german" / "de" -> "de", matching voices like "de_DE").
LANGUAGE_LOCALES = {
    'english': 'en', 'en': 'en',
    'german': 'de', 'deutsch': 'de', 'de': 'de',
}

# Preferred 'say' voices per locale prefix, in order of quality. The first
# one found installed (via 'say -v ?') is used; if none are installed, falls
# back to the first voice matching the locale prefix (see voice_for_language).
VOICE_PREFERENCES = {
    'de': ['Anna (Premium)', 'Anna (Enhanced)', 'Anna'],
    'ja': ['Otoya (Enhanced)', 'Kyoko (Enhanced)', 'Otoya', 'Kyoko'],
}

_VOICE_CACHE = {}


# --- Helper Functions ---
def clear_screen():
    """Clears the terminal screen."""
    os.system('cls' if os.name == 'nt' else 'clear')


def voice_for_language(lang):
    """Finds an installed macOS 'say' voice for lang, if any.

    English uses the system default voice (no '-v' flag). Other languages
    prefer a voice from VOICE_PREFERENCES if one is installed, otherwise the
    first installed voice matching the locale prefix (e.g. "de_DE")."""
    lang_lower = lang.lower()
    locale_prefix = LANGUAGE_LOCALES.get(lang_lower) or LANGUAGE_LOCALES.get(lang_lower.split('_')[0])
    if not locale_prefix or locale_prefix == 'en':
        return None
    if locale_prefix not in _VOICE_CACHE:
        voice = None
        try:
            output = subprocess.run(['say', '-v', '?'], capture_output=True, text=True, timeout=5).stdout
            installed = []
            for line in output.splitlines():
                match = re.match(r'^(.+?)\s+([a-zA-Z]{2}_[a-zA-Z]{2})\s+#', line)
                if match:
                    installed.append((match.group(1).strip(), match.group(2).lower()))
            for preferred in VOICE_PREFERENCES.get(locale_prefix, []):
                if any(name == preferred for name, _ in installed):
                    voice = preferred
                    break
            if not voice:
                for name, locale in installed:
                    if locale.startswith(locale_prefix):
                        voice = name
                        break
        except Exception:
            voice = None
        _VOICE_CACHE[locale_prefix] = voice
    return _VOICE_CACHE[locale_prefix]


def speak(text, lang=None, block=False, wpm=128):
    """Pipes text to the macOS 'say' command, using a voice matching lang's
    locale if one is installed. block=True waits for speech to finish.
    wpm sets the speech rate in words per minute (default 128, clear
    for language learners)."""
    rate = str(int(wpm)) if wpm else '128'
    cmd = ['say', '-r', rate]
    if lang:
        voice = voice_for_language(lang)
        if voice:
            cmd += ['-v', voice]
    cmd.append(text)
    try:
        if block:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def sanitize_name(name, label):
    """Validates a user/language name for safe use in table and file names."""
    name = name.lower()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid {label} '{name}': only lowercase letters, digits, and underscores are allowed."
        )
    return name


# --- Database Helpers ---
def get_connection():
    return sqlite3.connect(DATABASE_FILE)


def words_table_name(user, lang):
    return f"words_{sanitize_name(user, 'user')}_{sanitize_name(lang, 'language')}"


def sessions_table_name(user):
    return f"sessions_{sanitize_name(user, 'user')}"


def ensure_word_table(conn, user, lang):
    table = words_table_name(user, lang)
    sentence_table = 'sentences' in sanitize_name(lang, 'language')
    score_type = 'INTEGER' if sentence_table else 'REAL'
    default_score = SENTENCE_MIN_SCORE if sentence_table else 5.0
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL UNIQUE,
            definition TEXT NOT NULL DEFAULT '',
            word_frequency INTEGER,
            score {score_type} NOT NULL DEFAULT {default_score},
            last_practiced DATE,
            last_decay_at DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            last_fast_review_at TEXT,
            selection_order REAL
        )
    ''')
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if 'last_decay_at' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_decay_at DATE')
    if 'leitner_box' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN leitner_box INTEGER NOT NULL DEFAULT 1')
    if 'last_known_review_at' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_known_review_at TEXT')
    if 'last_fast_review_at' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_fast_review_at TEXT')
    if 'word_frequency' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN word_frequency INTEGER')
    if 'selection_order' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN selection_order REAL')
    conn.execute(
        f'UPDATE "{table}" SET selection_order = random() / 9223372036854775807.0 '
        'WHERE selection_order IS NULL'
    )
    if sentence_table:
        score_column = next((row for row in conn.execute(f'PRAGMA table_info("{table}")') if row[1] == 'score'), None)
        if score_column and score_column[2].upper() != 'INTEGER':
            migrate_sentence_score_to_integer(conn, table)
    conn.execute(
        f'UPDATE "{table}" SET last_decay_at = COALESCE(last_practiced, ?) WHERE last_decay_at IS NULL',
        (date.today().isoformat(),)
    )
    # One-time repair: reset leitner_box to 1 for any word with score < 9.
    # The box only advances on mastery (score = 9) and resets on incorrect.
    # A word with score < 9 and box > 1 is stale data from the old decay code
    # that lowered scores without resetting boxes. This is safe and idempotent:
    # it only runs when there's actually a mismatch, and it never touches
    # mastered words (score >= 9).
    conn.execute(
        f'UPDATE "{table}" SET leitner_box = 1 WHERE score < 9.0 AND leitner_box > 1'
    )
    return table


def migrate_sentence_score_to_integer(conn, table):
    """Rebuild a sentence table so score has INTEGER affinity."""
    tmp = f'{table}__score_int'
    conn.execute(f'DROP TABLE IF EXISTS "{tmp}"')
    conn.execute(f'''
        CREATE TABLE "{tmp}" (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL UNIQUE,
            definition TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            last_practiced DATE,
            last_decay_at DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER NOT NULL DEFAULT 1,
            last_known_review_at TEXT,
            last_fast_review_at TEXT
        )
    ''')
    conn.execute(f'''
        INSERT INTO "{tmp}" (
            id, text, definition, score, last_practiced, last_decay_at, active,
            times_practiced, times_correct, times_incorrect, times_drilled,
            times_mastered, leitner_box, last_known_review_at, last_fast_review_at
        )
        SELECT
            id, text, definition, CAST(ROUND(score) AS INTEGER), last_practiced,
            last_decay_at, active, times_practiced, times_correct,
            times_incorrect, times_drilled, times_mastered,
            leitner_box, last_known_review_at, last_fast_review_at
        FROM "{table}"
    ''')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')


def ensure_sessions_table(conn, user):
    table = sessions_table_name(user)
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            language TEXT NOT NULL,
            session_date DATE NOT NULL,
            duration_seconds INTEGER NOT NULL,
            words_practiced INTEGER NOT NULL,
            correct_count INTEGER NOT NULL,
            incorrect_count INTEGER NOT NULL,
            drilled_count INTEGER NOT NULL DEFAULT 0
        )
    ''')
    return table


# --- Word List Sync ---
def word_list_path(user, lang):
    """Resolve a user's list, then the categorized shared list.

    Shared lists live under ``data/word_lists/<language>/<kind>/`` while
    user-created lists remain at the word-list root for compatibility.
    """
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    user_specific = os.path.join(WORD_LISTS_DIR, f"{user}_{lang}.json")
    if os.path.isfile(user_specific):
        return user_specific

    legacy = os.path.join(WORD_LISTS_DIR, f"{lang}.json")
    if os.path.isfile(legacy):
        return legacy

    base_language = lang.split('_', 1)[0]
    kind = 'sentences' if is_sentence_list(lang) else 'vocabulary'
    parts = lang.split('_')
    level_index = 2 if kind == 'sentences' else 1
    level = parts[level_index] if len(parts) > level_index else ''
    if level in {'a1', 'a2', 'b1', 'b2', 'c1', 'c2'}:
        return os.path.join(WORD_LISTS_DIR, base_language, kind, level, f"{lang}.json")
    return os.path.join(WORD_LISTS_DIR, base_language, kind, f"{lang}.json")


def word_list_path_user_specific(user, lang):
    """Returns the user-specific word list path (for creating new lists)."""
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    return os.path.join(WORD_LISTS_DIR, f"{user}_{lang}.json")


def normalize_definition(definition):
    """Normalizes a definition (string, list of strings, or None) into newline-joined text."""
    if not definition:
        return ''
    if isinstance(definition, list):
        return '\n'.join(str(item).strip() for item in definition if str(item).strip())
    return str(definition).strip()


def normalize_word_frequency(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def apply_decay(conn, table):
    """
    Applies time-based decay: any active word not practiced for one or more
    days loses 1.0 score per idle day (floored at 1.0). This pulls neglected
    words back into easier question bands automatically.

    Mastered words (score >= 9.0) are exempt: they are governed by the Leitner
    spaced-repetition schedule, not by decay. Decaying them while they wait for
    their scheduled review would pull them back into easier bands before the
    review interval has elapsed, defeating the purpose of the box system.

    Leitner box integrity: any word with score < 9 must be in box 1. The box
    only advances on mastery (score reaching 9) and resets on an incorrect
    answer (which drops the score below 9). Since decay now only affects
    words already below 9, the box should already be 1 — but we enforce it
    here as a safety net to repair any stale boxes left over from the old
    decay code that lowered scores without resetting boxes.
    """
    today = date.today()
    cursor = conn.execute(
        f'SELECT id, score, last_decay_at, leitner_box FROM "{table}" WHERE active = 1 AND score > 1 AND score < 9 AND last_decay_at IS NOT NULL'
    )
    for word_id, score, last_decay_at, box in cursor.fetchall():
        last_decay_date = date.fromisoformat(last_decay_at)
        days = (today - last_decay_date).days
        if days >= 1:
            new_score = max(1.0, score - days)
            if box and box > 1:
                conn.execute(
                    f'UPDATE "{table}" SET score = ?, last_decay_at = ?, leitner_box = 1 WHERE id = ?',
                    (new_score, today.isoformat(), word_id)
                )
            else:
                conn.execute(
                    f'UPDATE "{table}" SET score = ?, last_decay_at = ? WHERE id = ?',
                    (new_score, today.isoformat(), word_id)
                )


def sync_word_list(user, lang, apply_score_decay=True):
    """
    Renews a user's word table from their word list JSON file:
    - new words are added (score 1.0, active)
    - existing words have their definition refreshed and are reactivated
    - words no longer present in the file are deactivated (history kept)
    Also applies time-based score decay for words left idle a week or more.
    """
    path = word_list_path(user, lang)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Word list not found: {path}\nRun: tartarus init --user {user} --lang {lang}"
        )
    with open(path, 'r', encoding='utf-8') as f:
        entries = json.load(f)

    conn = get_connection()
    table = ensure_word_table(conn, user, lang)
    ensure_sessions_table(conn, user)
    sentence_mode = is_sentence_list(lang)
    if not sentence_mode and apply_score_decay:
        apply_decay(conn, table)

    seen_words = set()
    for entry in entries:
        word = str(entry.get('word', '')).strip()
        if not word:
            continue
        seen_words.add(word)
        definition = normalize_definition(entry.get('definition'))
        word_frequency = normalize_word_frequency(entry.get('word_frequency'))
        cursor = conn.execute(f'SELECT id FROM "{table}" WHERE text = ?', (word,))
        row = cursor.fetchone()
        if row is None:
            conn.execute(
                f'INSERT INTO "{table}" (text, definition, word_frequency, score, active, selection_order) VALUES (?, ?, ?, ?, 1, ?)',
                (word, definition, word_frequency, SENTENCE_MIN_SCORE if sentence_mode else 1.0, random.random())
            )
        else:
            conn.execute(
                f'UPDATE "{table}" SET definition = ?, word_frequency = ?, active = 1 WHERE id = ?',
                (definition, word_frequency, row[0])
            )

    cursor = conn.execute(f'SELECT id, text FROM "{table}" WHERE active = 1')
    for word_id, text in cursor.fetchall():
        if text not in seen_words:
            conn.execute(f'UPDATE "{table}" SET active = 0 WHERE id = ?', (word_id,))

    conn.commit()
    conn.close()


# --- Practice / Scoring Logic ---
# Score bands determine which question type a word gets, Memrise-style:
# the lower a word's score, the more support it gets; the higher, the
# harder the question and the bigger the reward for getting it right.
MAX_QUESTIONS = 16   # unique words per session (each asked exactly once)
DRILL_WORDS = 10     # top-N most-incorrect words shown in drill mode

LEITNER_INTERVALS = {1: 1, 2: 2, 3: 4, 4: 9, 5: 14}  # box -> days until next review

SCORE_DELTAS = {1: 1.0, 2: 2.0, 3: 3.0}  # band -> score gained on a correct answer
INCORRECT_DELTA = 2.0        # score lost in band 1 or 2 on an incorrect answer
BAND3_INCORRECT_DELTA = 1.0  # score lost in band 3 on an incorrect answer
FIXED_SCORES = {
    'mastered': 9.0,
    'flagged': 1.0,
    'drilled': 5.0,
}
RESULT_COUNTERS = {
    'correct': 'times_correct',
    'incorrect': 'times_incorrect',
    'mastered': 'times_mastered',
    'drilled': 'times_drilled',
}

# --- Sentence practice ---
# Sentence lists (lang name contains "sentences") use a different practice
# flow from single-word lists: the native sentence is always shown, score is
# an integer 0..9 count of successful typings, and mistakes retry the same
# sentence without drill.
SENTENCE_MIN_SCORE = 0
SENTENCE_MAX_SCORE = 9
SENTENCE_CORRECT_DELTA = 1


def _corrects_to_mastery(score, sentence_mode=False):
    """Number of correct answers needed to bring score from current value to 9.0.

    Word mode: +1 in band 1 (score 1-3), +2 in band 2 (4-6), +3 in band 3 (7-9).
    Sentence mode: +1 per correct, so a new sentence needs 9 correct typings.
    """
    s, count = float(score), 0
    if sentence_mode:
        while s < 9.0:
            s = min(9.0, s + SENTENCE_CORRECT_DELTA)
            count += 1
        return count
    while s < 9.0:
        s = min(9.0, s + (3.0 if s >= 7 else 2.0 if s >= 4 else 1.0))
        count += 1
    return count


def corrects_to_mastery(score, sentence_mode=False):
    """Public version of _corrects_to_mastery for external use (e.g., web dashboard)."""
    return _corrects_to_mastery(score, sentence_mode)


def is_sentence_list(lang):
    """Returns True if the lang name identifies a sentence practice list."""
    return 'sentences' in (lang or '').lower()


def score_band(score):
    """Maps a word's score (1-9) to its question band: 1 (1-3), 2 (4-6), or 3 (7-9)."""
    if score >= 7:
        return 3
    if score >= 4:
        return 2
    return 1


def score_gauge(score, ansi=True):
    """Returns a 3-dot growth gauge for a word's score.
    If ansi=True (default), includes ANSI color codes for terminal.
    If ansi=False, returns plain Unicode dots for web."""
    if score >= 9:
        return '●●●' if not ansi else f"{Colors.GREEN}●●●{Colors.ENDC}"
    if score >= 7:
        return '●●○' if not ansi else f"{Colors.GREEN}●●○{Colors.ENDC}"
    if score >= 4:
        return '●○○' if not ansi else f"{Colors.YELLOW}●○○{Colors.ENDC}"
    return '○○○' if not ansi else f"{Colors.RED}○○○{Colors.ENDC}"


def get_gender_style(word_text):
    """Returns gender styling for a word based on German article.
    Returns tuple: (ansi_color, css_class) where ansi_color is for terminal,
    css_class is for web ('masc', 'fem', 'neut', 'none')."""
    text_lower = word_text.lower()
    if text_lower.startswith("der "):
        return Colors.BLUE, 'masc'
    if text_lower.startswith("die "):
        return Colors.RED, 'fem'
    if text_lower.startswith("das "):
        return Colors.GREEN, 'neut'
    return Colors.GREEN, 'none'


DRILL_TARGET = 9


def build_question_data(word_id, word_text, definition, score, leitner_box=1,
                         sentence_mode=False, fast_mode=False, drill_mode=False, known_drill_mode=False):
    """Builds the question data dict used by both CLI and web UI."""
    band = score_band(score)
    has_def = bool(definition)

    # Apply progressive masking for sentence mode
    display_word = word_text if fast_mode else (mask_sentence(word_text, int(round(score))) if sentence_mode else word_text)

    if fast_mode:
        question_type = 'fast'
    elif sentence_mode and not drill_mode and not known_drill_mode:
        question_type = 'learning' if has_def else 'spelling'
    elif band == 1:
        question_type = 'learning' if has_def else 'spelling'
    elif band == 2:
        question_type = 'audio'
    else:
        question_type = 'production'

    if known_drill_mode:
        question_type = 'known_review'

    full_definition_lines = definition.split('\n') if definition else []
    primary_definition = english_definition_only(definition)
    prompt_definition_lines = [primary_definition] if primary_definition else []
    definition_lines = full_definition_lines if question_type == 'learning' else prompt_definition_lines

    ansi_color, css_class = get_gender_style(word_text)

    question = {
        'word_id': word_id,
        'word': display_word,
        'word_unmasked': word_text,
        'definition': definition_lines,
        'score': min(SENTENCE_MAX_SCORE, int(round(score)) + 1) if sentence_mode else round(score, 1),
        'gauge': score_gauge(score, ansi=False),
        'band': band,
        'gender': css_class,
        'type': question_type,
        'sentence_mode': sentence_mode,
        'fast_mode': fast_mode,
    }
    initial_drill = None

    if drill_mode or known_drill_mode:
        definition_lines = prompt_definition_lines
        question['definition'] = definition_lines
        question['type'] = 'drill'
        question_type = 'drill'
        initial_drill = {'correct_in_a_row': 0, 'repetition': 1}
        question['drill_start'] = {
            'word': display_word,
            'definition': definition_lines,
            'repetition': 1,
            'correct_in_a_row': 0,
            'target': DRILL_TARGET,
            'show_word': not known_drill_mode,
        }

    return question, initial_drill


def record_as_drilled(user, lang, word_id, known_review=False):
    """Record a completed drill: increment times_drilled and erase one incorrect mark."""
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    set_clauses = [
        'times_drilled = times_drilled + 1',
        'times_practiced = times_practiced + 1',
        'times_incorrect = MAX(0, times_incorrect - 1)',
        'last_practiced = ?',
        'last_decay_at = ?',
    ]
    params = [today, today]
    if known_review:
        set_clauses.append('last_known_review_at = ?')
        params.append(now)
    params.append(word_id)
    conn.execute(
        f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE id = ?',
        params
    )
    conn.commit()
    conn.close()


def record_review_result(user, lang, word_id, correct):
    """Record a review-only answer without changing score or Leitner state."""
    table = words_table_name(user, lang)
    conn = get_connection()
    counter = 'times_correct' if correct else 'times_incorrect'
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET '
        f'times_practiced = times_practiced + 1, '
        f'{counter} = {counter} + 1, '
        f'last_practiced = ?, last_decay_at = ?, last_known_review_at = ? '
        f'WHERE id = ?',
        (today, today, now, word_id)
    )
    conn.commit()
    conn.close()


def record_known_review_seen(user, lang, word_id):
    """Mark a known-review word as seen without changing score or answer counters."""
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET '
        f'times_practiced = times_practiced + 1, '
        f'last_practiced = ?, last_decay_at = ?, last_known_review_at = ? '
        f'WHERE id = ?',
        (today, today, now, word_id)
    )
    conn.commit()
    conn.close()


def ensure_fast_review_column(conn, user, lang):
    """Add the Fast mode review marker without changing word progress."""
    table = words_table_name(user, lang)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    if exists:
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if 'last_fast_review_at' not in columns:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_fast_review_at TEXT')
    return table


def record_fast_review(user, lang, word_id):
    """Mark a completed Fast mode item without changing score or counters."""
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_fast_review_column(conn, user, lang)
    now = datetime.now().isoformat(timespec='microseconds')
    conn.execute(
        f'UPDATE "{table}" SET last_fast_review_at = ? WHERE id = ?',
        (now, word_id)
    )
    conn.commit()
    conn.close()


def update_word_score(user, lang, word_id, result_status, current_score=None, current_box=None):
    """Updates a word's score + Leitner box and increments its history counters.

    correct/incorrect: score computed from current_score; box advances or resets.
    mastered/flagged/drilled: fixed score; box set to 5/1/1 respectively.

    Leitner integrity: a word already mastered (score 9) that is practiced again
    on the SAME day is NOT a genuine review. Its box must not advance and its
    last_practiced timestamp (the anchor for the box's review schedule) must not
    be overwritten. Only the first transition into mastery, or a real due review
    (last practiced on a prior day), may advance the box. This prevents gaming
    the system by re-practicing a word repeatedly within one day to fast-forward
    it through the boxes."""
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()

    row = conn.execute(f'SELECT last_practiced FROM "{table}" WHERE id = ?', (word_id,)).fetchone()
    stored_last_practiced = row[0] if row else None
    practiced_today = (stored_last_practiced == today)

    preserve_box_timestamp = False

    if result_status == 'correct':
        new_score = min(9.0, current_score + SCORE_DELTAS[score_band(current_score)])
        just_mastered = (current_score < 9.0) and (new_score >= 9.0)
        if just_mastered:
            # First transition into mastery: advance the box and stamp today.
            new_box = min((current_box or 1) + 1, 5)
        elif current_score >= 9.0:
            # Already mastered — this is a review. Only a genuine due review
            # (practiced on a prior day) advances the box. Same-day re-practice
            # must NOT advance the box or overwrite last_practiced.
            if practiced_today:
                new_box = current_box or 1
                preserve_box_timestamp = True
            else:
                new_box = min((current_box or 1) + 1, 5)
        else:
            # Intermediate correct: score improves, box unchanged.
            new_box = current_box or 1
    elif result_status == 'incorrect':
        delta = BAND3_INCORRECT_DELTA if score_band(current_score) == 3 else INCORRECT_DELTA
        new_score = max(1.0, current_score - delta)
        new_box = 1
    else:
        new_score = FIXED_SCORES[result_status]
        # Box rules for manual overrides:
        #   mastered -> 5 (long-term memory, 14-day review)
        #   flagged  -> 1 (struggling, daily review)
        #   drilled  -> 1 (score drops to 5.0 which is below mastery, so the
        #                  box must reset to 1 — otherwise the word would have
        #                  score < 9 with a high box, and re-mastering it would
        #                  skip Leitner boxes)
        new_box = {'mastered': 5, 'flagged': 1, 'drilled': 1}[result_status]

    counter = RESULT_COUNTERS.get(result_status)
    if new_box is not None and not preserve_box_timestamp:
        set_clauses = ['score = ?', 'leitner_box = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, new_box, today, today]
    elif preserve_box_timestamp:
        # Same-day re-practice of an already-mastered word: bump counters only.
        # Do NOT touch leitner_box, last_practiced or last_decay_at.
        set_clauses = ['score = ?', 'times_practiced = times_practiced + 1']
        params = [new_score]
    else:
        set_clauses = ['score = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, today, today]
    if counter:
        set_clauses.append(f'{counter} = {counter} + 1')
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE id = ?', params)
    conn.commit()
    conn.close()


def update_sentence_score(user, lang, word_id, correct, current_score=None, current_box=None):
    """Sentence-specific scoring: integer progress from 1 to 9.

    - Correct: score += 1 (capped at 9). When score first hits 9, the Leitner
      box advances. Same-day re-practice of an already-mastered sentence does
      not advance the box or overwrite last_practiced.
    - Incorrect: score, box, last_practiced, and mistake counters are unchanged;
      callers must retry the same sentence.
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()

    row = conn.execute(f'SELECT last_practiced FROM "{table}" WHERE id = ?', (word_id,)).fetchone()
    stored_last_practiced = row[0] if row else None
    practiced_today = (stored_last_practiced == today)

    preserve_box_timestamp = False

    if correct:
        current = int(current_score or SENTENCE_MIN_SCORE)
        new_score = min(SENTENCE_MAX_SCORE, current + SENTENCE_CORRECT_DELTA)
        just_mastered = current < SENTENCE_MAX_SCORE and new_score >= SENTENCE_MAX_SCORE
        if just_mastered:
            new_box = min((current_box or 1) + 1, 5)
        elif current >= SENTENCE_MAX_SCORE:
            if practiced_today:
                new_box = current_box or 1
                preserve_box_timestamp = True
            else:
                new_box = min((current_box or 1) + 1, 5)
        else:
            new_box = current_box or 1
        counter = 'times_correct'
    else:
        conn.close()
        return

    if preserve_box_timestamp:
        set_clauses = ['score = ?', 'times_practiced = times_practiced + 1']
        params = [new_score]
    else:
        set_clauses = ['score = ?', 'leitner_box = ?', 'last_practiced = ?', 'last_decay_at = ?',
                       'times_practiced = times_practiced + 1']
        params = [new_score, new_box, today, today]
    set_clauses.append(f'{counter} = {counter} + 1')
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE id = ?', params)
    conn.commit()
    conn.close()


def get_words_for_practice(user, lang, num_words=MAX_QUESTIONS, drill_mode=False, known_drill_mode=False):
    """
    Normal mode — daily practice is capped per file:
      Priority 0: In-progress words (score < 9) that are new, practiced today,
                  or Leitner-due. The candidate pool is selected by frequency,
                  then ordered for the session by score band and frequency.
                  Higher frequency counts are more common, so common words are
                  introduced before less frequent words. Once a word hits 9 it
                  leaves this group for the day (it was mastered today and
                  last_practiced = today, so it won't match here again).
      Priority 1: Mastered words (score 9) whose Leitner interval has elapsed
                   AND were NOT practiced today — genuine scheduled reviews.
      No filler: mastered words that were practiced today, or whose interval
      hasn't elapsed yet, are excluded. This forces the user to move to another
      file or use drill/known-drill once they're done with today's words.

    Drill mode — most mistaken words first (scores unchanged).
    Known drill mode — never-reviewed known words first from oldest trained to
    newest trained, then previously reviewed words from oldest review to newest.
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    if known_drill_mode:
        cursor = conn.execute(
            f'''SELECT id, text, definition, score, leitner_box, word_frequency FROM "{table}"
                WHERE active = 1 AND score >= 9.0 AND times_practiced > 0
                ORDER BY
                  CASE WHEN last_known_review_at IS NULL THEN 0 ELSE 1 END,
                  CASE
                    WHEN last_known_review_at IS NULL THEN datetime(last_practiced)
                    ELSE datetime(last_known_review_at)
                  END ASC,
                  id ASC
                LIMIT ?''',
            (num_words,)
        )
    elif drill_mode:
        cursor = conn.execute(
            f'''SELECT id, text, definition, score, leitner_box, word_frequency FROM "{table}"
                WHERE active = 1 AND times_incorrect > 0
                ORDER BY times_incorrect DESC, last_practiced ASC
                LIMIT ?''',
            (num_words,)
        )
    else:
        # Normal mode: only show words that are legitimately practiceable today.
        # A word is practiceable if:
        #   - it's in-progress (score < 9): ALWAYS practiceable. In-progress
        #     words are never gated by the Leitner interval — the box only
        #     controls when a MASTERED word comes back for review. A word that
        #     decayed back below 9 must be practiced again regardless of how
        #     recently it was last practiced.
        #   - it's mastered (score >= 9) AND Leitner-due AND not practiced today.
        # Mastered words practiced today are excluded (they're done for the day).
        # Not-yet-due mastered words are excluded (their interval hasn't elapsed).
        # This caps daily practice per file and pushes the user to other files
        # or drill modes once today's words are done.
        cursor = conn.execute(
            f'''WITH candidates AS (
                  SELECT id, text, definition, score, leitner_box, word_frequency, selection_order
                  FROM "{table}"
                  WHERE active = 1 AND (
                    score < 9
                    OR
                    (score >= 9 AND (
                      last_practiced IS NULL
                      OR date(last_practiced) < date('now', 'localtime')
                    ) AND (
                      last_practiced IS NULL
                      OR julianday('now', 'localtime') - julianday(last_practiced) >=
                         CASE leitner_box WHEN 1 THEN 1 WHEN 2 THEN 2
                                          WHEN 3 THEN 4 WHEN 4 THEN 9 ELSE 14 END
                    ))
                  )
                  ORDER BY CASE WHEN word_frequency IS NULL THEN 1 ELSE 0 END,
                           word_frequency DESC, selection_order ASC, id ASC
                  LIMIT ?
                )
                SELECT id, text, definition, score, leitner_box, word_frequency
                FROM candidates
                ORDER BY score DESC,
                         CASE WHEN word_frequency IS NULL THEN 1 ELSE 0 END,
                         word_frequency DESC, selection_order ASC, id ASC''',
            (num_words,)
        )
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        if known_drill_mode:
            raise ValueError(
                "No known practiced words to review. Master some words first, then try this mode again."
            )
        if drill_mode:
            raise ValueError(
                "No words with mistakes to drill. Keep practicing and errors will show up here."
            )
        # Normal mode: no practiceable words. Check if there are active words
        # at all, to give the user a more helpful message.
        check_conn = get_connection()
        has_active = check_conn.execute(
            f'SELECT 1 FROM "{table}" WHERE active = 1 LIMIT 1'
        ).fetchone()
        check_conn.close()
        if has_active:
            raise ValueError(
                "All words in this list are mastered for today.\n"
                "Come back tomorrow to review them, or use drill mode / known-drill\n"
                "to keep practicing. You can also switch to another word list."
            )
        raise ValueError(
            "No active words found for this list. Add words to your word list file and try again."
        )
    return rows


def get_mastered_words_for_fast(user, lang):
    """Return mastered words in Fast mode order, oldest review first."""
    table = words_table_name(user, lang)
    conn = get_connection()
    ensure_fast_review_column(conn, user, lang)
    rows = conn.execute(
        f'''SELECT id, text, definition, score, leitner_box
            FROM "{table}"
            WHERE active = 1 AND score >= 9.0
            ORDER BY
              CASE WHEN last_fast_review_at IS NULL THEN 0 ELSE 1 END,
              datetime(last_fast_review_at) ASC,
              id ASC'''
    ).fetchall()
    conn.close()
    if not rows:
        raise ValueError("No mastered words are available for fast mode.")
    return rows


def show_definition(definition):
    """Prints each line of a (possibly multi-line) definition, indented and highlighted."""
    if not definition:
        return
    for line in definition.split('\n'):
        print(f"  {Colors.CYAN}{line}{Colors.ENDC}")


def english_definition_only(definition):
    """
    Returns the primary English prompt line, excluding sample sentences.
    Generated vocabulary lists store the core definition first and examples
    later; lines with " — " keep only the English side.
    """
    if not definition:
        return ''
    for line in definition.split('\n'):
        line = line.strip()
        if not line:
            continue
        if ' — ' in line:
            return line.rsplit(' — ', 1)[1].strip()
        return line
    return ''


def drill_word(user, lang, word_to_drill, word_id, definition, header_text, audio, audio_lang=None, update_score=True, wpm=128, show_word=True):
    """Initiates a strict 9-repetition drill with a consistent single-line UI."""
    clear_screen()
    print(header_text)
    if show_word:
        print(f"--- Drill Mode: '{get_gender_color(word_to_drill)}{word_to_drill}{Colors.ENDC}' ---")
    else:
        print("--- Known Drill Mode ---")
    prompt_definition = english_definition_only(definition)
    if prompt_definition:
        show_definition(prompt_definition)
    print("")
    correct_in_a_row = 0
    while correct_in_a_row < 9:
        sys.stdout.write('\033[A')
        erase_line = "\r\033[K"
        drill_header = f"Repetition {correct_in_a_row + 1}/9: "
        sys.stdout.write(f"{erase_line}{drill_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_to_drill, audio_lang or lang, wpm=wpm)
        answer = input("").strip()
        sys.stdout.write('\033[A' + erase_line)
        if answer_matches(answer, word_to_drill):
            correct_in_a_row += 1
            print(f"{drill_header} Correct! ({correct_in_a_row}/9)")
        else:
            correct_in_a_row = 0
            print(f"{drill_header} Incorrect. Drill resetting.")
    print("\n--- Drill Complete. ---")
    if update_score:
        update_word_score(user, lang, word_id, 'drilled')
        print("Score set to 5.0.")
    time.sleep(1)


ERASE_LINE = "\r\033[K"

SESSION_HELP_SENTENCE = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '?' (repeat), '+' (replay audio)."
SESSION_HELP = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '$' (drill), '?' (repeat), '+' (replay audio)."



def handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=None, sentence_mode=False):
    """
    Checks an answer for the session-level special commands. Returns
    (status, message) if one matched ('end'/'drilled'/'mastered'/'flagged'),
    or None if the answer should be checked normally for correctness.

    In sentence_mode the '$' drill command is disabled (sentences are too
    long to drill).
    """
    if answer == '!!':
        return 'end', None, None
    if answer.startswith('$'):
        if sentence_mode:
            return None
        drill_word(user, lang, word_text, word_id, definition, header_text, audio, audio_lang=audio_lang)
        return 'drilled', None, None
    if answer.startswith('@'):
        update_word_score(user, lang, word_id, 'mastered')
        return 'mastered', f"Marked '{word_text}' as known.", None
    if answer.startswith('!'):
        update_word_score(user, lang, word_id, 'flagged')
        return 'flagged', f"Flagged '{word_text}' for more practice.", None
    return None


def ask_learning(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, sentence_mode=False, wpm=128):
    """
    Band 1 (score 1-3): the word and its definition(s) are both shown - this
    is recognition practice for words you're still learning. If the word has
    no definition, falls back to a flash-then-hide spelling test instead.
    Correct -> +1, incorrect -> -2.

    In sentence_mode: the native sentence is shown with progressive masking
    (score 1-4: 100% visible, score 5: 75%, score 6: 50%, score 7: 25%,
    score 8: 12.5%, score 9: 6.25%). Each correct answer adds 1 score point,
    and incorrect answers retry the same sentence without score/box penalty.
    Drill ('$') is disabled.
    """
    while True:
        clear_screen()
        print(header_text)
        print("")
        has_def = bool(definition)
        if has_def:
            # In sentence mode, show masked sentence based on score
            if sentence_mode:
                display_text = mask_sentence(word_text, int(round(score)))
            else:
                display_text = word_text
            print(f"{get_gender_color(display_text)}{display_text}{Colors.ENDC}")
            show_definition(definition)
            print("")
            while True:
                sys.stdout.write(f"{ERASE_LINE}{word_header} ")
                sys.stdout.flush()
                if audio:
                    speak(word_text, audio_lang or lang, wpm=wpm)
                answer = input("").strip()
                sys.stdout.write('\033[A' + ERASE_LINE)
                if answer == '?':
                    if sentence_mode:
                        reveal_text = mask_sentence(word_text, int(round(score)))
                    else:
                        reveal_text = word_text
                    sys.stdout.write(f"{word_header} {get_gender_color(reveal_text)}{reveal_text}{Colors.ENDC}")
                    sys.stdout.flush()
                    time.sleep(1.0)
                    sys.stdout.write(ERASE_LINE)
                    continue
                if answer == '+':
                    continue
                break
        else:
            while True:
                if sentence_mode:
                    display_text = mask_sentence(word_text, int(round(score)))
                else:
                    display_text = word_text
                sys.stdout.write(f"{ERASE_LINE}{word_header} {get_gender_color(display_text)}{display_text}{Colors.ENDC}")
                sys.stdout.flush()
                if audio:
                    speak(word_text, audio_lang or lang, wpm=wpm)
                time.sleep(0.6)
                sys.stdout.write(f"{ERASE_LINE}{word_header} ")
                sys.stdout.flush()
                answer = input("").strip()
                sys.stdout.write('\033[A' + ERASE_LINE)
                if answer == '?' or answer == '+':
                    continue
                break

        special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=audio_lang, sentence_mode=sentence_mode)
        if special:
            return special

        correct = answer_matches(answer, word_text, sentence_mode=sentence_mode)
        if update_score:
            if sentence_mode:
                update_sentence_score(user, lang, word_id, correct, score, current_box)
            else:
                update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
        if audio:
            speak(word_text, audio_lang or lang, wpm=wpm)
        if correct:
            return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
        if not sentence_mode:
            return 'incorrect', f"Incorrect. The word was: {Colors.RED}{word_text}{Colors.ENDC}", answer
        print(f"{word_header} {Colors.RED}Incorrect. Try one more time.{Colors.ENDC}")
        time.sleep(1.2)


def ask_audio(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, wpm=128):
    """
    Band 2 (score 4-6): nothing is shown - listen to the word's audio and
    type it from memory. '?' replays the audio and briefly shows the word.
    Correct -> +2, incorrect -> -2.
    """
    clear_screen()
    print(header_text)
    print("")
    print(f"{Colors.YELLOW}Listen and type the word you hear.{Colors.ENDC} ('?' to repeat the audio and briefly show the word)\n")
    while True:
        sys.stdout.write(f"{ERASE_LINE}{word_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_text, audio_lang or lang, wpm=wpm)
        answer = input("").strip()
        sys.stdout.write('\033[A' + ERASE_LINE)
        if answer == '?':
            sys.stdout.write(f"{word_header} {get_gender_color(word_text)}{word_text}{Colors.ENDC}")
            sys.stdout.flush()
            time.sleep(1.0)
            sys.stdout.write(ERASE_LINE)
            continue
        if answer == '+':
            continue
        break

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=audio_lang)
    if special:
        return special

    correct = answer_matches(answer, word_text)
    if update_score:
        update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
    if audio:
        speak(word_text, audio_lang or lang, wpm=wpm)
    if correct:
        return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
    return 'incorrect', f"Incorrect. The word was: {Colors.RED}{word_text}{Colors.ENDC}", answer


def ask_production(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, wpm=128):
    """
    Band 3 / drill-mode question: definition is shown and audio plays; the
    user must type the word from memory (case-sensitive). When update_score
    is False the caller is responsible for recording the attempt (drill mode).
    """
    clear_screen()
    print(header_text)
    print(f"\n{Colors.YELLOW}Type the word from the definition and audio.{Colors.ENDC} ('?' to replay)\n")
    prompt_definition = english_definition_only(definition)
    if prompt_definition:
        show_definition(prompt_definition)
    print("")

    while True:
        sys.stdout.write(f"{ERASE_LINE}{word_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_text, audio_lang or lang, wpm=wpm)
        answer = input("").strip()
        sys.stdout.write('\033[A' + ERASE_LINE)
        if answer == '?':
            if prompt_definition:
                show_definition(prompt_definition)
            if audio:
                speak(word_text, audio_lang or lang, wpm=wpm)
            continue
        if answer == '+':
            if audio:
                speak(word_text, audio_lang or lang, wpm=wpm)
            continue
        break

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer, audio_lang=audio_lang)
    if special:
        return special

    correct = answer_matches(answer, word_text)
    if update_score:
        update_word_score(user, lang, word_id, 'correct' if correct else 'incorrect', score, current_box)
    if audio:
        speak(word_text, audio_lang or lang, wpm=wpm)  # replay after answer
    if correct:
        return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
    return 'incorrect', f"Incorrect. The word was: {Colors.RED}{word_text}{Colors.ENDC}", answer


def start_fast_practice_session(user, lang, audio, audio_lang=None, wpm=128):
    """Run the CLI Fast mode without changing word scores or practice counters."""
    sync_word_list(user, lang)
    sentence_mode = is_sentence_list(lang)
    rows = get_mastered_words_for_fast(user, lang)
    start_time = time.time()
    correct_count = 0
    incorrect_list = []
    queue = list(rows)

    try:
        for index, (word_id, word_text, definition, score, current_box) in enumerate(queue, 1):
            clear_screen()
            print(f"--- Fast Mode | Q{index}/{len(queue)} ---")
            print("The word is shown. Type it from memory; mistakes retry the same word.")
            print(f"  {word_text}")
            prompt_definition = english_definition_only(definition)
            if prompt_definition:
                show_definition(prompt_definition)
            if audio:
                speak(word_text, audio_lang or lang, wpm=wpm)

            while True:
                answer = input("Answer: ").strip()
                if answer == '!!':
                    raise KeyboardInterrupt
                if answer == '?':
                    if prompt_definition:
                        show_definition(prompt_definition)
                    if audio:
                        speak(word_text, audio_lang or lang, wpm=wpm)
                    continue
                if answer == '+':
                    if audio:
                        speak(word_text, audio_lang or lang, wpm=wpm)
                    continue
                if answer_matches(answer, word_text, sentence_mode=sentence_mode):
                    record_fast_review(user, lang, word_id)
                    correct_count += 1
                    print("Correct.")
                    break
                incorrect_list.append((word_text, answer))
                print("Incorrect. Try again.")
    except KeyboardInterrupt:
        print("\n\nFast session ended early. Saving progress...")

    if correct_count == 0:
        print("No words were completed. Nothing to save.")
        return

    elapsed_seconds = int(time.time() - start_time)
    log_session(user, lang, elapsed_seconds, correct_count, correct_count,
                len(incorrect_list), 0)
    clear_screen()
    attempts = correct_count + len(incorrect_list)
    minutes, seconds = divmod(elapsed_seconds, 60)
    print("\n--- Fast Session Summary ---")
    print(f"Words completed:     {correct_count}")
    print(f"Incorrect answers:   {len(incorrect_list)}")
    print(f"Accuracy:            {100 * correct_count / attempts:.1f}%")
    print(f"Session time:        {minutes} min {seconds} sec")
    print("\nFast session finished. Progress saved.")


def start_practice_session(user, lang, audio, audio_lang=None, drill_all=False, drill_mode=False, instant_drill=False, known_drill_mode=False, wpm=128):
    """
    Up to MAX_QUESTIONS unique words per session using Leitner spaced repetition.
    Due words (box interval elapsed) come first; each word is asked exactly once.
    Correct → advance one Leitner box. Incorrect → reset to box 1.

    Sentence lists (lang contains "sentences") always use the sentence flow
    regardless of score: the native sentence is shown, score advances by
    exactly 1 per correct answer from 1 to 9, and drill is disabled.
    """
    sentence_mode = is_sentence_list(lang)
    if sentence_mode and (drill_all or drill_mode or known_drill_mode or instant_drill):
        raise ValueError("Sentence lists do not support drill modes.")
    words = get_words_for_practice(user, lang, DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS, drill_mode=drill_mode, known_drill_mode=known_drill_mode)
    queue = [{'id': r[0], 'word': r[1], 'def': r[2], 'score': r[3], 'box': r[4]}
             for r in words]

    correct_count = 0
    questions_count = 0
    drilled_words_count = 0
    incorrect_list = []
    start_time = time.time()
    total = len(queue)
    mode_label = " [DRILL ALL]" if drill_all else ""
    help_text = SESSION_HELP_SENTENCE if sentence_mode else SESSION_HELP

    def header_text():
        return (
            f"--- Practice{mode_label} | "
            f"Q{questions_count}/{total} | "
            f"Correct: {correct_count} ---\n{help_text}"
        )

    try:
        for entry in queue:
            word_id, word_text, definition, score, current_box = (
                entry['id'], entry['word'], entry['def'], entry['score'], entry['box']
            )
            display_score = min(SENTENCE_MAX_SCORE, int(round(score)) + 1) if sentence_mode else score
            word_header = f"{score_gauge(score)} (score: {display_score:.1f}):"
            band = score_band(score)

            if drill_all:
                drill_word(user, lang, word_text, word_id, definition,
                           header_text(), audio, audio_lang=audio_lang, wpm=wpm)
                status, message, attempt = 'drilled', None, None
            elif drill_mode:
                drill_word(user, lang, word_text, word_id, definition,
                           header_text(), audio, audio_lang=audio_lang,
                           update_score=False, wpm=wpm)
                status, message, attempt = 'drilled', None, None
            elif known_drill_mode:
                drill_word(user, lang, word_text, word_id, definition,
                           header_text(), audio, audio_lang=audio_lang,
                           update_score=False, wpm=wpm, show_word=False)
                record_as_drilled(user, lang, word_id, known_review=True)
                status, message, attempt = 'drilled', None, None
            elif sentence_mode:
                status, message, attempt = ask_learning(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    current_box=current_box, sentence_mode=True, wpm=wpm)
            elif band == 1:
                status, message, attempt = ask_learning(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    current_box=current_box, wpm=wpm)
            elif band == 2:
                status, message, attempt = ask_audio(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    current_box=current_box, wpm=wpm)
            else:
                status, message, attempt = ask_production(
                    user, lang, word_id, word_text, definition, score,
                    audio, header_text(), word_header, audio_lang=audio_lang,
                    update_score=True, current_box=current_box, wpm=wpm)

            if status == 'end':
                print("\n\nSession ended early. Saving progress...")
                break

            questions_count += 1

            if drill_mode:
                record_as_drilled(user, lang, word_id)
                drilled_words_count += 1
                if message:
                    print(f"{word_header} {message}")
                    time.sleep(1.2)
                continue

            if status == 'drilled':
                drilled_words_count += 1
            elif status == 'correct':
                correct_count += 1
            elif status == 'incorrect':
                incorrect_list.append((word_text, attempt))
                if instant_drill:
                    drill_word(user, lang, word_text, word_id, definition,
                               header_text(), audio, audio_lang=audio_lang,
                               update_score=False, wpm=wpm)
                    record_as_drilled(user, lang, word_id)
                    drilled_words_count += 1

            if message:
                print(f"{word_header} {message}")
                time.sleep(1.2)

    except KeyboardInterrupt:
        print("\n\nSession ended early (Ctrl+C). Saving progress...")

    if questions_count == 0:
        clear_screen()
        print("No words were practiced. Nothing to save.")
        return

    elapsed_seconds = int(time.time() - start_time)
    log_session(user, lang, elapsed_seconds, questions_count, correct_count,
                len(incorrect_list), drilled_words_count)
    clear_screen()
    print("\n--- Session Summary ---")
    minutes, seconds = divmod(elapsed_seconds, 60)
    print(f"Questions answered:  {questions_count}")
    print(f"Correct answers:     {correct_count}")
    print(f"Incorrect answers:   {len(incorrect_list)}")
    print(f"Words drilled:       {drilled_words_count}")
    print(f"Session time:        {minutes} min {seconds} sec")
    if incorrect_list:
        print("\nWords you got wrong:")
        for word, attempt in incorrect_list:
            print(f"  - You wrote: '{attempt}', correct: '{word}'")
    print("\nSession finished. Progress saved.")


# --- Reporting ---
def log_session(user, lang, duration, practiced, correct, incorrect, drilled):
    conn = get_connection()
    table = ensure_sessions_table(conn, user)
    conn.execute(
        f'INSERT INTO "{table}" (language, session_date, duration_seconds, words_practiced, '
        f'correct_count, incorrect_count, drilled_count) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (lang, date.today().isoformat(), duration, practiced, correct, incorrect, drilled)
    )
    conn.commit()
    conn.close()


def print_language_report(conn, table, language):
    where_clause, params = "WHERE language = ?", [language]

    query = (
        f'SELECT session_date, COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
        f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
        f'FROM "{table}" {where_clause} GROUP BY session_date ORDER BY session_date DESC'
    )
    cursor = conn.execute(query, params)
    report_data = cursor.fetchall()
    if not report_data:
        return False

    print(f"\n--- Daily Practice Report ({language}) ---")
    header_format = "{:<12} | {:<10} | {:<12} | {:<15} | {:<15} | {:<15} | {:<15} | {:<15}"
    header = header_format.format(
        "Date", "Sessions", "Spent Time", "Practiced Words", "Correct Words",
        "Wrong Words", "Drilled Words", "Avg Time/Word"
    )
    print(header)
    print("-" * len(header))
    for row in report_data:
        s_date, sessions, seconds, practiced, correct, incorrect, drilled = row
        minutes, sec = divmod(seconds, 60)
        time_str = f"{minutes}m {sec}s"
        avg_time_str = f"{(seconds / practiced):.1f}s" if practiced > 0 else "N/A"
        print(header_format.format(s_date, sessions, time_str, practiced, correct, incorrect or 0, drilled or 0, avg_time_str))

    total_query = (
        f'SELECT COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
        f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
        f'FROM "{table}" {where_clause}'
    )
    cursor = conn.execute(total_query, params)
    t_sessions, t_seconds, t_practiced, t_correct, t_incorrect, t_drilled = cursor.fetchone()
    print("-" * len(header))
    if t_seconds is not None:
        t_hours, rem = divmod(t_seconds, 3600)
        t_minutes, _ = divmod(rem, 60)
        total_time_str = f"{t_hours}h {t_minutes}m"
        total_avg_time_str = f"{(t_seconds / t_practiced):.1f}s" if t_practiced > 0 else "N/A"
        print(header_format.format("Total", t_sessions, total_time_str, t_practiced, t_correct, t_incorrect or 0, t_drilled or 0, total_avg_time_str))
    return True


def compute_streak(date_strings):
    """Return (current_streak, best_streak) from a list of ISO date strings."""
    if not date_strings:
        return 0, 0
    parsed = sorted({date.fromisoformat(d) for d in date_strings})
    today = date.today()
    yesterday = today - timedelta(days=1)
    date_set = set(parsed)

    # Current streak: walk backwards from today (or yesterday if today has none)
    start = today if today in date_set else (yesterday if yesterday in date_set else None)
    current = 0
    if start:
        check = start
        while check in date_set:
            current += 1
            check -= timedelta(days=1)

    # Best streak: scan sorted dates for longest consecutive run
    best, run, prev = 0, 0, None
    for d in parsed:
        run = run + 1 if (prev is not None and d == prev + timedelta(days=1)) else 1
        best = max(best, run)
        prev = d

    return current, best


def print_user_report(conn, table, user):
    """Print an aggregate daily report across all languages for the user."""
    rows = conn.execute(
        f'SELECT session_date, COUNT(id), COUNT(DISTINCT language), '
        f'SUM(duration_seconds), SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}" GROUP BY session_date ORDER BY session_date DESC'
    ).fetchall()
    if not rows:
        return False

    all_dates = conn.execute(f'SELECT session_date FROM "{table}"').fetchall()
    current_streak, best_streak = compute_streak([r[0] for r in all_dates])

    totals = conn.execute(
        f'SELECT COUNT(id), COUNT(DISTINCT language), SUM(duration_seconds), '
        f'SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}"'
    ).fetchone()

    print(f"\n{'=' * 72}")
    print(f"  User Report: {user}")
    print(f"{'=' * 72}")
    print(f"  Streak  ›  Current: {current_streak} day{'s' if current_streak != 1 else ''}   "
          f"Best: {best_streak} day{'s' if best_streak != 1 else ''}")

    hfmt = "{:<12} | {:<8} | {:<9} | {:<10} | {:<8} | {:<8} | {:<7} | {:<9} | {:<9}"
    header = hfmt.format("Date", "Sessions", "Languages", "Time", "Words", "Correct", "Wrong", "Accuracy", "Avg/Word")
    print(f"\n--- Daily Summary (All Languages) ---")
    print(header)
    print("-" * len(header))
    for s_date, sessions, langs, seconds, practiced, correct, incorrect in rows:
        minutes, sec = divmod(seconds or 0, 60)
        time_str = f"{minutes}m {sec}s"
        total_ans = (correct or 0) + (incorrect or 0)
        accuracy = f"{100 * correct / total_ans:.0f}%" if total_ans > 0 else "N/A"
        avg = f"{seconds / practiced:.1f}s" if practiced else "N/A"
        print(hfmt.format(s_date, sessions, langs, time_str, practiced or 0, correct or 0, incorrect or 0, accuracy, avg))

    t_sessions, t_langs, t_seconds, t_practiced, t_correct, t_incorrect = totals
    print("-" * len(header))
    t_h, t_rem = divmod(t_seconds or 0, 3600)
    t_m, _ = divmod(t_rem, 60)
    t_time = f"{t_h}h {t_m}m"
    t_total_ans = (t_correct or 0) + (t_incorrect or 0)
    t_accuracy = f"{100 * t_correct / t_total_ans:.0f}%" if t_total_ans > 0 else "N/A"
    t_avg = f"{t_seconds / t_practiced:.1f}s" if t_practiced else "N/A"
    print(hfmt.format("Total", t_sessions, t_langs, t_time, t_practiced or 0, t_correct or 0, t_incorrect or 0, t_accuracy, t_avg))
    return True


def print_due_summary(conn, user, lang):
    """Print Leitner box distribution and due-today count for a word list."""
    table = words_table_name(user, lang)
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return
    rows = conn.execute(
        f'''SELECT leitner_box, COUNT(*) AS total,
            SUM(CASE WHEN last_practiced IS NULL
                     OR date(last_practiced) = date('now', 'localtime')
                     OR julianday('now', 'localtime') - julianday(last_practiced) >=
                        CASE leitner_box WHEN 1 THEN 1 WHEN 2 THEN 2
                                         WHEN 3 THEN 4 WHEN 4 THEN 9 ELSE 14 END
                THEN 1 ELSE 0 END) AS due
            FROM "{table}" WHERE active = 1
            GROUP BY leitner_box ORDER BY leitner_box''',
        ()
    ).fetchall()
    if not rows:
        return
    total_due = sum(r[2] or 0 for r in rows)
    total_words = sum(r[1] for r in rows)
    box_str = '  '.join(f"Box {r[0]}: {r[2] or 0}/{r[1]}" for r in rows)
    print(f"\nReview Status  Active: {total_words}  Due today: {total_due}")
    print(f"  {box_str}  (due/total per box)")


def generate_report(user, lang=None):
    user_s = sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        print("No practice sessions found.")
        conn.close()
        return

    if lang:
        languages = [sanitize_name(lang, 'language')]
    else:
        print_user_report(conn, table, user_s)
        cursor = conn.execute(f'SELECT DISTINCT language FROM "{table}" ORDER BY language')
        languages = [row[0] for row in cursor.fetchall()]

    any_data = False
    for language in languages:
        if print_language_report(conn, table, language):
            any_data = True
            if lang:
                print_due_summary(conn, user_s, language)
    if not any_data:
        print("No practice sessions found.")
    conn.close()


# --- CLI ---
def cmd_init(args):
    os.makedirs(WORD_LISTS_DIR, exist_ok=True)
    path = word_list_path_user_specific(args.user, args.lang)
    if os.path.exists(path):
        print(f"Word list already exists: {path}")
    else:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=2, ensure_ascii=False)
        print(f"Created empty word list: {path}")
    conn = get_connection()
    ensure_word_table(conn, args.user, args.lang)
    ensure_sessions_table(conn, args.user)
    conn.commit()
    conn.close()
    print(f"Initialized tables for user '{args.user}', language '{args.lang}'.")
    print(f"Add entries to {path} (each as {{\"word\": ..., \"definition\": ...}}), then run 'practice'.")


def cmd_practice(args):
    audio = sys.platform == 'darwin' and not args.no_audio
    if args.fast:
        if args.drill or args.drill_mode or args.instant_drill or args.known_drill_mode:
            raise ValueError("Fast mode cannot be combined with drill modes.")
        start_fast_practice_session(args.user, args.lang, audio,
                                     audio_lang=args.audio_lang or None,
                                     wpm=args.wpm)
        return
    sync_word_list(args.user, args.lang)
    start_practice_session(args.user, args.lang, audio,
                           audio_lang=args.audio_lang or None,
                           drill_all=args.drill,
                           drill_mode=args.drill_mode,
                           instant_drill=args.instant_drill,
                           known_drill_mode=args.known_drill_mode,
                           wpm=args.wpm)


def cmd_report(args):
    if args.lang:
        sync_word_list(args.user, args.lang)
    generate_report(args.user, args.lang)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='tartarus',
        description="An interactive CLI tool for vocabulary practice with multi-user, multi-language word lists.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Usage Examples:
  # First time setup for a user/language (creates word_lists/<user>_<lang>.json)
  make init user=bahman list=german

  # Start a practice session (4 words, 16 questions); audio on by default on macOS
  make practice user=bahman list=german

  # Same, but without audio
  make practice user=bahman list=german opts="--no-audio"

  # View progress report
  make report user=bahman list=german

How question types are chosen:
  Every word has a score from 1.0 (struggling) to 9.0 (mastered). Each
  session, every word's CURRENT score picks its question type, so a session
  over a mix of new and practiced words naturally mixes all three:
    score 1-3 (o o o)  Learning - word + definition(s) shown, type the word.
                        Correct: +1.
    score 4-6 (* o o)  Audio    - listen only, type the word you hear.
                        Correct: +2.
    score 7-9 (* * o/*) Production - definition shown, type the word.
                        Correct: +3 (capped at 9.0).
  Any incorrect answer: -2 (floored at 1.0). Words left idle for one or
  more days also lose 1.0 per idle day automatically, pulling them back into
  easier question types over time.

Special Commands (during a session):
  !! or Ctrl+C  -> End session early and save progress.
  ?             -> See the word again / replay its audio.
  +             -> Replay the current word's audio.
  !word         -> Flag word as difficult (score becomes 1.0).
  @word         -> Mark word as known (score becomes 9.0).
  $word         -> Start a strict 9-repetition drill for the current word
                    (score becomes 5.0 afterwards).

Developed by Bahman Farhadian.
"""
    )
    subparsers = parser.add_subparsers(dest='command')

    practice_parser = subparsers.add_parser('practice', help="Start a practice session.")
    practice_parser.add_argument('--user', required=True, help="Username (lowercase letters, digits, underscores).")
    practice_parser.add_argument('--lang', required=True, help="Word list / language to practice.")
    practice_parser.add_argument('--no-audio', action='store_true',
                                  help="Disable speaking each word aloud (audio is on by default on macOS, via 'say';\n"
                                       "has no effect on other platforms). Tartarus tries to use a 'say' voice that\n"
                                       "matches --lang (e.g. a German voice for --lang german).")
    practice_parser.add_argument('--audio-lang',
                                  help="Override the language used for voice/audio selection.\n"
                                       "Useful when --lang is a sub-list name (e.g. 'german_home') that doesn't\n"
                                       "auto-detect as a language: pass --audio-lang german to still use the\n"
                                       "German 'say' voice. Accepts the same values as --lang (e.g. 'german', 'de').")
    practice_parser.add_argument('--fast', action='store_true',
                                  help="Fast mode: review mastered words in oldest-fast-review order; scores unchanged.")
    practice_parser.add_argument('--drill', action='store_true',
                                  help="Drill-mode: every word in the session is put through the 9-repetition\n"
                                       "drill automatically, regardless of its score band.")
    practice_parser.add_argument('--drill-mode', action='store_true',
                                  help="Review drill: practice your high-mistake words without changing\n"
                                       "their scores. Completing a drill reduces that word's mistake count.")
    practice_parser.add_argument('--instant-drill', action='store_true',
                                  help="Instant drill: after any incorrect answer, immediately start a\n"
                                       "9-repetition drill for that word (score unchanged).")
    practice_parser.add_argument('--known-drill-mode', action='store_true',
                                  help="Known drill: review mastered words that were never reviewed,\n"
                                       "then oldest review first. Completing a drill reduces mistake count.")
    practice_parser.add_argument('--wpm', type=int, default=128,
                                  help="Speech rate in words per minute for macOS 'say' (default 128;\n"
                                       "clear for language learners; lower = slower, higher = faster).")

    report_parser = subparsers.add_parser('report', help="Show practice history.")
    report_parser.add_argument('--user', required=True, help="Username.")
    report_parser.add_argument('--lang', help="Limit the report to a single language (default: all languages).")

    init_parser = subparsers.add_parser('init', help="Create a new word list and its tables for a user/language.")
    init_parser.add_argument('--user', required=True, help="Username.")
    init_parser.add_argument('--lang', required=True, help="Language / word list name.")

    return parser


def main():
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
    args = parser.parse_args()
    try:
        if args.command == 'practice':
            cmd_practice(args)
        elif args.command == 'report':
            cmd_report(args)
        elif args.command == 'init':
            cmd_init(args)
        else:
            parser.print_help()
    except Exception as e:
        print(f"\n{Colors.RED}An error occurred: {e}{Colors.ENDC}")
        sys.exit(1)


if __name__ == "__main__":
    main()
