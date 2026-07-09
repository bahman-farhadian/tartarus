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
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
DATABASE_FILE = os.path.join(DATA_DIR, 'mashq.db')
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
    case-insensitively (comma-separated forms like "das Haus, die Häuser").
    Also accepts the full text with all forms typed out, e.g.
    "das Haus, die Häuser", however the commas/spacing are written.

    In sentence_mode, commas are part of the sentence and must NOT be treated
    as form separators — a simple case-insensitive full-string comparison is
    used instead."""
    if sentence_mode:
        return answer.strip().lower() == word_text.strip().lower()
    forms = [form.strip().lower() for form in split_word_forms(word_text)]
    answer_forms = [form.strip().lower() for form in split_word_forms(answer)]
    if len(answer_forms) == 1 and answer_forms[0] in forms:
        return True
    return sorted(answer_forms) == sorted(forms)


def get_gender_color(word_text):
    """Returns a color for a word based on its German article, if any:
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
    'french': 'fr', 'francais': 'fr', 'fr': 'fr',
    'spanish': 'es', 'espanol': 'es', 'es': 'es',
    'italian': 'it', 'it': 'it',
    'dutch': 'nl', 'nl': 'nl',
    'portuguese': 'pt', 'pt': 'pt',
    'russian': 'ru', 'ru': 'ru',
    'japanese': 'ja', 'ja': 'ja',
    'hiragana': 'ja', 'kanji': 'ja', 'katakana': 'ja',
    'chinese': 'zh', 'zh': 'zh',
    'korean': 'ko', 'ko': 'ko',
    'turkish': 'tr', 'tr': 'tr',
    'polish': 'pl', 'pl': 'pl',
    'swedish': 'sv', 'sv': 'sv',
    'norwegian': 'no', 'no': 'no',
    'danish': 'da', 'da': 'da',
    'arabic': 'ar', 'ar': 'ar',
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


def speak(text, lang=None, block=False, wpm=64):
    """Pipes text to the macOS 'say' command, using a voice matching lang's
    locale if one is installed. block=True waits for speech to finish.
    wpm sets the speech rate in words per minute (default 64, slow and clear
    for language learners)."""
    rate = str(int(wpm)) if wpm else '64'
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
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL UNIQUE,
            definition TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL DEFAULT 5.0,
            last_practiced DATE,
            last_decay_at DATE,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_flagged INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0
        )
    ''')
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if 'last_decay_at' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_decay_at DATE')
    if 'leitner_box' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN leitner_box INTEGER NOT NULL DEFAULT 1')
    if 'last_known_review_at' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN last_known_review_at TEXT')
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


def sync_word_list(user, lang):
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
            f"Word list not found: {path}\nRun: mashq init --user {user} --lang {lang}"
        )
    with open(path, 'r', encoding='utf-8') as f:
        entries = json.load(f)

    conn = get_connection()
    table = ensure_word_table(conn, user, lang)
    ensure_sessions_table(conn, user)
    apply_decay(conn, table)

    seen_words = set()
    for entry in entries:
        word = str(entry.get('word', '')).strip()
        if not word:
            continue
        seen_words.add(word)
        definition = normalize_definition(entry.get('definition'))
        cursor = conn.execute(f'SELECT id FROM "{table}" WHERE text = ?', (word,))
        row = cursor.fetchone()
        if row is None:
            conn.execute(
                f'INSERT INTO "{table}" (text, definition, score, active) VALUES (?, ?, 1.0, 1)',
                (word, definition)
            )
        else:
            conn.execute(
                f'UPDATE "{table}" SET definition = ?, active = 1 WHERE id = ?',
                (definition, row[0])
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
    'flagged': 'times_flagged',
    'mastered': 'times_mastered',
    'drilled': 'times_drilled',
}

# --- Sentence practice ---
# Sentence lists (lang name contains "sentences") use a different practice
# flow from single-word lists: the native sentence is always shown, each
# sentence needs 10 correct answers to reach mastery (score 9.0), and
# mistakes do not trigger drills since sentences are too long to drill.
SENTENCE_CORRECTS_TO_MASTER = 10
SENTENCE_CORRECT_DELTA = 8.0 / SENTENCE_CORRECTS_TO_MASTER  # 0.8 per correct -> 9.0 after 10


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


def score_gauge(score):
    """Returns a 3-dot growth gauge for a word's score: o..o (1-3), filled..o (4-6), etc."""
    if score >= 9:
        return f"{Colors.GREEN}●●●{Colors.ENDC}"
    if score >= 7:
        return f"{Colors.GREEN}●●○{Colors.ENDC}"
    if score >= 4:
        return f"{Colors.YELLOW}●○○{Colors.ENDC}"
    return f"{Colors.RED}○○○{Colors.ENDC}"


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


def update_word_score(user, lang, word_id, result_status, current_score=None, current_box=None):
    """Updates a word's score + Leitner box and increments its history counters.

    correct/incorrect: score computed from current_score; box advances or resets.
    mastered/flagged/drilled: fixed score; box set to 5/1/unchanged respectively.

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
    """Sentence-specific scoring: 10 correct answers to reach mastery.

    - Correct: score += 0.8 (capped at 9.0). When score first hits 9.0, the
      Leitner box advances. Same-day re-practice of an already-mastered
      sentence does NOT advance the box or overwrite last_practiced (same
      anti-cheating rule as update_word_score).
    - Incorrect: score is NOT reduced (sentences are long; a single typo
      should not erase progress). The box resets to 1 only if the sentence
      was already mastered (score >= 9).
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    today = date.today().isoformat()

    row = conn.execute(f'SELECT last_practiced FROM "{table}" WHERE id = ?', (word_id,)).fetchone()
    stored_last_practiced = row[0] if row else None
    practiced_today = (stored_last_practiced == today)

    preserve_box_timestamp = False

    if correct:
        new_score = min(9.0, (current_score or 1.0) + SENTENCE_CORRECT_DELTA)
        just_mastered = (current_score or 1.0) < 9.0 and new_score >= 9.0
        if just_mastered:
            new_box = min((current_box or 1) + 1, 5)
        elif (current_score or 1.0) >= 9.0:
            if practiced_today:
                new_box = current_box or 1
                preserve_box_timestamp = True
            else:
                new_box = min((current_box or 1) + 1, 5)
        else:
            new_box = current_box or 1
        counter = 'times_correct'
    else:
        new_score = current_score or 1.0
        if (current_score or 1.0) >= 9.0:
            new_box = 1
        else:
            new_box = current_box or 1
        counter = 'times_incorrect'

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
                  or Leitner-due — ordered by score DESC. These are the words
                  the user is actively learning today. Once a word hits 9 it
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
            f'''SELECT id, text, definition, score, leitner_box FROM "{table}"
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
            f'''SELECT id, text, definition, score, leitner_box FROM "{table}"
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
            f'''SELECT id, text, definition, score, leitner_box FROM "{table}"
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
                ORDER BY
                  CASE WHEN score < 9 THEN 0 ELSE 1 END,
                  score DESC,
                  last_practiced ASC
                LIMIT ?''',
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


def drill_word(user, lang, word_to_drill, word_id, definition, header_text, audio, audio_lang=None, update_score=True, wpm=64):
    """Initiates a strict 9-repetition drill with a consistent single-line UI."""
    clear_screen()
    print(header_text)
    print(f"--- Drill Mode: '{get_gender_color(word_to_drill)}{word_to_drill}{Colors.ENDC}' ---")
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


def ask_learning(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, sentence_mode=False, wpm=64):
    """
    Band 1 (score 1-3): the word and its definition(s) are both shown - this
    is recognition practice for words you're still learning. If the word has
    no definition, falls back to a flash-then-hide spelling test instead.
    Correct -> +1, incorrect -> -2.

    In sentence_mode: the native sentence is always shown (never hidden), each
    correct answer adds SENTENCE_CORRECT_DELTA, and incorrect answers do not
    reduce the score. Drill ('$') is disabled.
    """
    clear_screen()
    print(header_text)
    print("")
    has_def = bool(definition)
    if has_def:
        print(f"{get_gender_color(word_text)}{word_text}{Colors.ENDC}")
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
                sys.stdout.write(f"{word_header} {get_gender_color(word_text)}{word_text}{Colors.ENDC}")
                sys.stdout.flush()
                time.sleep(1.0)
                sys.stdout.write(ERASE_LINE)
                continue
            if answer == '+':
                continue
            break
    else:
        while True:
            sys.stdout.write(f"{ERASE_LINE}{word_header} {get_gender_color(word_text)}{word_text}{Colors.ENDC}")
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
    return 'incorrect', f"Incorrect. The word was: {Colors.RED}{word_text}{Colors.ENDC}", answer


def ask_audio(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, wpm=64):
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


def ask_production(user, lang, word_id, word_text, definition, score, audio, header_text, word_header, audio_lang=None, update_score=True, current_box=1, wpm=64):
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


def start_practice_session(user, lang, audio, audio_lang=None, drill_all=False, drill_mode=False, wpm=64):
    """
    Up to MAX_QUESTIONS unique words per session using Leitner spaced repetition.
    Due words (box interval elapsed) come first; each word is asked exactly once.
    Correct → advance one Leitner box. Incorrect → reset to box 1.

    Sentence lists (lang contains "sentences") always use the "learning"
    question flow regardless of score: the native sentence is shown, 10
    corrects are needed for mastery, and drill is disabled.
    """
    sentence_mode = is_sentence_list(lang)
    words = get_words_for_practice(user, lang, DRILL_WORDS if drill_mode else MAX_QUESTIONS, drill_mode=drill_mode)
    queue = [{'id': r[0], 'word': r[1], 'def': r[2], 'score': r[3], 'box': r[4]}
             for r in words]

    correct_count = 0
    questions_count = 0
    drilled_words_count = 0
    incorrect_list = []
    start_time = time.time()
    total = len(queue)
    mode_label = " [DRILL ALL]" if drill_all else ""
    help_text = SESSION_HELP_SENTENCE if (sentence_mode and not drill_all and not drill_mode) else SESSION_HELP

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
            word_header = f"{score_gauge(score)} (score: {score:.1f}):"
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
    path = word_list_path(args.user, args.lang)
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
    sync_word_list(args.user, args.lang)
    start_practice_session(args.user, args.lang, audio,
                           audio_lang=args.audio_lang or None,
                           drill_all=args.drill,
                           drill_mode=args.drill_mode,
                           wpm=args.wpm)


def cmd_report(args):
    if args.lang:
        sync_word_list(args.user, args.lang)
    generate_report(args.user, args.lang)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='mashq',
        description="An interactive CLI tool for vocabulary practice with multi-user, multi-language word lists.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Usage Examples:
  # First time setup for a user/language (creates word_lists/<user>_<lang>.json)
  ./mashq.sh init --user bahman --lang german

  # Start a practice session (4 words, 16 questions); audio on by default on macOS
  ./mashq.sh practice --user bahman --lang german

  # Same, but without audio
  ./mashq.sh practice --user bahman --lang german --no-audio

  # View progress report
  ./mashq.sh report --user bahman --lang german

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
                                       "has no effect on other platforms). Mashq tries to use a 'say' voice that\n"
                                       "matches --lang (e.g. a German voice for --lang german).")
    practice_parser.add_argument('--audio-lang',
                                  help="Override the language used for voice/audio selection.\n"
                                       "Useful when --lang is a sub-list name (e.g. 'german_home') that doesn't\n"
                                       "auto-detect as a language: pass --audio-lang german to still use the\n"
                                       "German 'say' voice. Accepts the same values as --lang (e.g. 'german', 'de').")
    practice_parser.add_argument('--drill', action='store_true',
                                  help="Drill-mode: every word in the session is put through the 9-repetition\n"
                                       "drill automatically, regardless of its score band.")
    practice_parser.add_argument('--drill-mode', action='store_true',
                                  help="Review drill: practice your high-mistake words without changing\n"
                                       "their scores. Completing a drill reduces that word's mistake count.")
    practice_parser.add_argument('--wpm', type=int, default=64,
                                  help="Speech rate in words per minute for macOS 'say' (default 64, slow and\n"
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
