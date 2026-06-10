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
import collections
from datetime import date

# --- Configuration ---
DATA_DIR = 'data'
DATABASE_FILE = os.path.join(DATA_DIR, 'lexiloop.db')
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


def answer_matches(answer, word_text):
    """Checks a typed answer against every accepted form of a word
    (case-insensitive, comma-separated forms like "das Haus, die Häuser").
    Also accepts the full text with all forms typed out, e.g.
    "das Haus, die Häuser", however the commas/spacing are written."""
    forms = [form.lower() for form in split_word_forms(word_text)]
    answer_forms = [form.lower() for form in split_word_forms(answer)]
    if len(answer_forms) == 1 and answer_forms[0] in forms:
        return True
    return answer_forms == forms


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
    locale_prefix = LANGUAGE_LOCALES.get(lang.lower())
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
                    installed.append((match.group(1), match.group(2).lower()))
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


def speak(text, lang=None):
    """Pipes text to the macOS 'say' command in the background, using a
    voice matching lang's locale if one is installed."""
    cmd = ['say']
    if lang:
        voice = voice_for_language(lang)
        if voice:
            cmd += ['-v', voice]
    cmd.append(text)
    try:
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
    conn.execute(
        f'UPDATE "{table}" SET last_decay_at = COALESCE(last_practiced, ?) WHERE last_decay_at IS NULL',
        (date.today().isoformat(),)
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
    Applies time-based decay: any active word not practiced in a full week
    or more loses 1.0 score per elapsed week (floored at 1.0). This pulls
    neglected words back into easier question bands automatically, replacing
    the old manual 'update' command.
    """
    today = date.today()
    cursor = conn.execute(
        f'SELECT id, score, last_decay_at FROM "{table}" WHERE active = 1 AND score > 1'
    )
    for word_id, score, last_decay_at in cursor.fetchall():
        last_decay_date = date.fromisoformat(last_decay_at)
        weeks = (today - last_decay_date).days // 7
        if weeks >= 1:
            new_score = max(1.0, score - weeks)
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
            f"Word list not found: {path}\nRun: lexiloop init --user {user} --lang {lang}"
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
SCORE_DELTAS = {1: 1.0, 2: 2.0, 3: 3.0}  # band -> score gained on a correct answer
INCORRECT_DELTA = 2.0  # score lost on any incorrect answer
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


def update_word_score(user, lang, word_id, result_status, current_score=None):
    """Updates a word's score and increments its history counters.

    For 'correct'/'incorrect', the new score is computed from current_score:
    a correct answer gains SCORE_DELTAS[score_band(current_score)], an
    incorrect answer loses INCORRECT_DELTA, both clamped to [1.0, 9.0].
    'mastered'/'flagged'/'drilled' set a fixed score regardless of history."""
    table = words_table_name(user, lang)
    conn = get_connection()
    if result_status == 'correct':
        new_score = min(9.0, current_score + SCORE_DELTAS[score_band(current_score)])
    elif result_status == 'incorrect':
        new_score = max(1.0, current_score - INCORRECT_DELTA)
    else:
        new_score = FIXED_SCORES[result_status]
    counter = RESULT_COUNTERS.get(result_status)
    set_clauses = ['score = ?', 'last_practiced = ?', 'last_decay_at = ?', 'times_practiced = times_practiced + 1']
    params = [new_score, date.today().isoformat(), date.today().isoformat()]
    if counter:
        set_clauses.append(f'{counter} = {counter} + 1')
    params.append(word_id)
    conn.execute(f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE id = ?', params)
    conn.commit()
    conn.close()


def get_words_for_practice(user, lang, num_words):
    """
    Selects active words for a session, prioritizing words with scores < 9,
    and filling remaining slots with the oldest-practiced mastered words.
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    cursor = conn.execute(
        f'SELECT id, text, definition, score FROM "{table}" '
        f'WHERE active = 1 AND score < 9 ORDER BY score ASC, last_practiced ASC'
    )
    priority_words = cursor.fetchall()
    random.shuffle(priority_words)

    cursor = conn.execute(
        f'SELECT id, text, definition, score FROM "{table}" '
        f'WHERE active = 1 AND score = 9 ORDER BY last_practiced ASC'
    )
    mastered_words = cursor.fetchall()
    random.shuffle(mastered_words)

    combined_pool = priority_words + mastered_words
    conn.close()
    if not combined_pool:
        raise ValueError(
            "No active words found for this list. Add words to your word list file and try again."
        )
    return combined_pool[:num_words]


def show_definition(definition):
    """Prints each line of a (possibly multi-line) definition, indented and highlighted."""
    if not definition:
        return
    for line in definition.split('\n'):
        print(f"  {Colors.CYAN}{line}{Colors.ENDC}")


def drill_word(user, lang, word_to_drill, word_id, definition, header_text, show_def, audio):
    """Initiates a strict 9-repetition drill with a consistent single-line UI."""
    clear_screen()
    print(header_text)
    print(f"--- Drill Mode: '{get_gender_color(word_to_drill)}{word_to_drill}{Colors.ENDC}' ---")
    if show_def and definition:
        show_definition(definition)
    print("")
    correct_in_a_row = 0
    while correct_in_a_row < 9:
        sys.stdout.write('\033[A')
        erase_line = "\r\033[K"
        drill_header = f"Repetition {correct_in_a_row + 1}/9: "
        sys.stdout.write(f"{erase_line}{drill_header} ")
        sys.stdout.flush()
        if audio:
            speak(word_to_drill, lang)
        answer = input("").strip()
        sys.stdout.write('\033[A' + erase_line)
        if answer_matches(answer, word_to_drill):
            correct_in_a_row += 1
            print(f"{drill_header} Correct! ({correct_in_a_row}/9)")
        else:
            correct_in_a_row = 0
            print(f"{drill_header} Incorrect. Drill resetting.")
    print("\n--- Drill Complete. Score set to 5.0. ---")
    update_word_score(user, lang, word_id, 'drilled')
    time.sleep(1)


ERASE_LINE = "\r\033[K"

SESSION_HELP = "Commands: '!!' or Ctrl+C (end), '!' (flag), '@' (master), '$' (drill), '?' (repeat), '+' (replay audio)."


def build_definition_pool(words_for_session):
    """Collects (word, definition line) pairs across the session, for multiple-choice distractors."""
    pool = []
    for _, word_text, definition, _ in words_for_session:
        if not definition:
            continue
        for line in definition.split('\n'):
            line = line.strip()
            if line:
                pool.append((word_text, line))
    return pool


def handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer):
    """
    Checks an answer for the session-level special commands. Returns
    (status, message) if one matched ('end'/'drilled'/'mastered'/'flagged'),
    or None if the answer should be checked normally for correctness.
    """
    if answer == '!!':
        return 'end', None
    if answer.startswith('$'):
        drill_word(user, lang, word_text, word_id, definition, header_text, True, audio)
        return 'drilled', None
    if answer.startswith('@'):
        update_word_score(user, lang, word_id, 'mastered')
        return 'mastered', f"Marked '{word_text}' as known."
    if answer.startswith('!'):
        update_word_score(user, lang, word_id, 'flagged')
        return 'flagged', f"Flagged '{word_text}' for more practice."
    return None


def ask_learning(user, lang, word_id, word_text, definition, score, audio, header_text, word_header):
    """
    Band 1 (score 1-3): the word and its definition(s) are both shown - this
    is recognition practice for words you're still learning. If the word has
    no definition, falls back to a flash-then-hide spelling test instead.
    Correct -> +1, incorrect -> -2.
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
                speak(word_text, lang)
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
                speak(word_text, lang)
            time.sleep(0.6)
            sys.stdout.write(f"{ERASE_LINE}{word_header} ")
            sys.stdout.flush()
            answer = input("").strip()
            sys.stdout.write('\033[A' + ERASE_LINE)
            if answer == '?' or answer == '+':
                continue
            break

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer)
    if special:
        return special + (None,)

    if answer_matches(answer, word_text):
        update_word_score(user, lang, word_id, 'correct', score)
        return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
    update_word_score(user, lang, word_id, 'incorrect', score)
    return 'incorrect', f"Incorrect. The word was: {Colors.RED}{word_text}{Colors.ENDC}", answer


def ask_audio(user, lang, word_id, word_text, definition, score, audio, header_text, word_header):
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
            speak(word_text, lang)
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

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer)
    if special:
        return special + (None,)

    if answer_matches(answer, word_text):
        update_word_score(user, lang, word_id, 'correct', score)
        return 'correct', f"{Colors.GREEN}{word_text}{Colors.ENDC}", None
    update_word_score(user, lang, word_id, 'incorrect', score)
    return 'incorrect', f"Incorrect. The word was: {Colors.RED}{word_text}{Colors.ENDC}", answer


def ask_meaning(user, lang, word_id, word_text, definition, score, definition_pool, audio, header_text, word_header):
    """
    Band 3 (score 7-9): the word (and its audio) is shown, and you pick its
    meaning from a multiple-choice list. If the word has no definition,
    falls back to ask_audio (listening dictation) instead, but the band 3
    score delta still applies. Correct -> +3 (capped at 9), incorrect -> -2.
    """
    if not definition:
        return ask_audio(user, lang, word_id, word_text, definition, score, audio, header_text, word_header)

    clear_screen()
    print(header_text)
    print("")
    print(f"{word_header} {get_gender_color(word_text)}{word_text}{Colors.ENDC}")
    if audio:
        speak(word_text, lang)
    print("")

    own_lines = [line.strip() for line in definition.split('\n') if line.strip()]
    correct_def = random.choice(own_lines)
    distractors = list(dict.fromkeys(
        d for w, d in definition_pool if w != word_text and d not in own_lines
    ))
    random.shuffle(distractors)
    options = [correct_def] + distractors[:3]
    random.shuffle(options)
    correct_letter = chr(ord('a') + options.index(correct_def))

    print("What does it mean?")
    for i, option in enumerate(options):
        print(f"  {chr(ord('a') + i)}) {option}")

    while True:
        answer = input("\nYour answer: ").strip()
        if answer == '?' or answer == '+':
            if audio:
                speak(word_text, lang)
            continue
        break

    special = handle_special_commands(user, lang, word_id, word_text, definition, header_text, audio, answer)
    if special:
        return special + (None,)

    choice = answer.lower()[:1]
    if choice == correct_letter:
        update_word_score(user, lang, word_id, 'correct', score)
        return 'correct', f"{Colors.GREEN}Correct!{Colors.ENDC}", None
    update_word_score(user, lang, word_id, 'incorrect', score)
    return 'incorrect', f"Incorrect. The right answer was {correct_letter}) {correct_def}", answer


def start_practice_session(user, lang, words_for_session, audio):
    """
    Single-pass practice session: each word's current score selects which
    question type it gets (Band 1 'Learning', Band 2 'Audio', Band 3
    'Meaning' - see ask_learning/ask_audio/ask_meaning), so a session over a
    mix of new and practiced words naturally mixes all three question types.
    """
    correct_count, words_practiced_count, drilled_words_count = 0, 0, 0
    incorrect_list = []
    total_words = len(words_for_session)
    header_text = f"--- Practice Session ({total_words} words) ---\n{SESSION_HELP}"
    definition_pool = build_definition_pool(words_for_session)
    start_time = time.time()
    queue = collections.deque(words_for_session)
    try:
        while queue:
            word_id, word_text, definition, score = queue.popleft()
            word_header = f"Word {words_practiced_count + 1}/{total_words} {score_gauge(score)} (score: {score:.1f}):"
            band = score_band(score)
            if band == 1:
                status, message, attempt = ask_learning(
                    user, lang, word_id, word_text, definition, score, audio, header_text, word_header
                )
            elif band == 2:
                status, message, attempt = ask_audio(
                    user, lang, word_id, word_text, definition, score, audio, header_text, word_header
                )
            else:
                status, message, attempt = ask_meaning(
                    user, lang, word_id, word_text, definition, score, definition_pool, audio, header_text, word_header
                )

            if status == 'end':
                print("\n\nSession ended early. Saving progress...")
                break

            words_practiced_count += 1
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
    if words_practiced_count == 0:
        clear_screen()
        print("No words were practiced. Nothing to save.")
        return
    elapsed_seconds = int(time.time() - start_time)
    log_session(user, lang, elapsed_seconds, words_practiced_count, correct_count, len(incorrect_list), drilled_words_count)
    clear_screen()
    print("\n--- Session Summary ---")
    minutes, seconds = divmod(elapsed_seconds, 60)
    print(f"Words practiced: {words_practiced_count}")
    print(f"Correct answers: {correct_count}")
    print(f"Incorrect answers: {len(incorrect_list)}")
    print(f"Words drilled: {drilled_words_count}")
    print(f"Session time: {minutes} min {seconds} sec")
    if incorrect_list:
        print("\nWords you got wrong:")
        for correct, attempt in incorrect_list:
            print(f"- You wrote: '{attempt}', Correct was: '{correct}'")
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
    if t_seconds:
        t_hours, rem = divmod(t_seconds, 3600)
        t_minutes, _ = divmod(rem, 60)
        total_time_str = f"{t_hours}h {t_minutes}m"
        total_avg_time_str = f"{(t_seconds / t_practiced):.1f}s" if t_practiced > 0 else "N/A"
        print(header_format.format("Total", t_sessions, total_time_str, t_practiced, t_correct, t_incorrect or 0, t_drilled or 0, total_avg_time_str))
    return True


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
        cursor = conn.execute(f'SELECT DISTINCT language FROM "{table}" ORDER BY language')
        languages = [row[0] for row in cursor.fetchall()]

    any_data = False
    for language in languages:
        if print_language_report(conn, table, language):
            any_data = True
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
    words_for_session = get_words_for_practice(args.user, args.lang, args.number)
    start_practice_session(args.user, args.lang, words_for_session, audio)


def cmd_report(args):
    if args.lang:
        sync_word_list(args.user, args.lang)
    generate_report(args.user, args.lang)


def build_parser():
    parser = argparse.ArgumentParser(
        prog='lexiloop',
        description="An interactive CLI tool for vocabulary practice with multi-user, multi-language word lists.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Usage Examples:
  # First time setup for a user/language (creates word_lists/<user>_<lang>.json)
  ./lexiloop.sh init --user bahman --lang german

  # Start a practice session; audio is on by default on macOS
  ./lexiloop.sh practice --user bahman --lang german --number 15

  # Same, but without audio
  ./lexiloop.sh practice --user bahman --lang german --no-audio

  # View progress report
  ./lexiloop.sh report --user bahman --lang german

How question types are chosen:
  Every word has a score from 1.0 (struggling) to 9.0 (mastered). Each
  session, every word's CURRENT score picks its question type, so a session
  over a mix of new and practiced words naturally mixes all three:
    score 1-3 (o o o)  Learning - word + definition(s) shown, type the word.
                        Correct: +1.
    score 4-6 (* o o)  Audio    - listen only, type the word you hear.
                        Correct: +2.
    score 7-9 (* * o/*) Meaning - word shown, pick its meaning (a-d).
                        Correct: +3 (capped at 9.0).
  Any incorrect answer: -2 (floored at 1.0). Words left idle for a week or
  more also lose 1.0 per idle week automatically, pulling them back into
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
    practice_parser.add_argument('--number', type=int, default=20, help="Number of words for the session (default: 20).")
    practice_parser.add_argument('--no-audio', action='store_true',
                                  help="Disable speaking each word aloud (audio is on by default on macOS, via 'say';\n"
                                       "has no effect on other platforms). LexiLoop tries to use a 'say' voice that\n"
                                       "matches --lang (e.g. a German voice for --lang german).")

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
