# -*- coding: utf-8 -*-
import contextlib
import os
import re
import sys
import json
import time
import random
import sqlite3
import subprocess
import logging
import logging.handlers
import shutil
import hashlib
import tempfile
import uuid
import threading
import unicodedata
from datetime import date, datetime, timedelta

# --- Configuration ---
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
DATABASE_FILE = os.environ.get('TARTARUS_DB', os.path.join(DATA_DIR, 'tartarus.db'))
WORD_LISTS_DIR = os.environ.get('TARTARUS_WORD_LISTS_DIR', os.path.join(DATA_DIR, 'word_lists'))
AUDIO_DIR = os.environ.get('TARTARUS_AUDIO_DIR', os.path.join(DATA_DIR, 'audio'))
LOG_FILE_PATH = os.path.join(PROJECT_DIR, 'tartarus.log')
NAME_PATTERN = re.compile(r'^[a-z0-9_\-\.!]+$')

# Logging is configured by executable entry points, not at import time. This keeps
# library calls and isolated tests free of project-log side effects.
logger = logging.getLogger('tartarus')
logger.addHandler(logging.NullHandler())


def configure_logging():
    """Configure bounded, redacted application logging once per process."""
    if getattr(logger, '_tartarus_configured', False):
        return
    level = getattr(logging, os.environ.get('TARTARUS_LOG_LEVEL', 'INFO').upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    log_path = os.environ.get('TARTARUS_LOG_FILE', LOG_FILE_PATH)
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger._tartarus_configured = True


def log_event(event_type, **kwargs):
    # Learner answers and correct targets are deliberately excluded from logs.
    sensitive = {'answer', 'typed', 'target', 'word_text'}
    details = ' | '.join(f"{key}: {value}" for key, value in kwargs.items() if key not in sensitive)
    logger.info(f"{event_type} | {details}")


def tts_available():
    """Return whether this host can provide the supported macOS speech engine."""
    return sys.platform == 'darwin' and shutil.which('say') is not None


class Colors:
    YELLOW = '\033[93m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    CYAN = '\033[96m'
    BLUE = '\033[94m'  # Masculine (der)
    ENDC = '\033[0m'


def answer_matches(answer, word_text):
    """Return True only for an exact dataset-target match.

    Learning content is deliberately strict: no trimming, case-folding,
    comma-form splitting/reordering, or fuzzy matching. The one exception
    (P6) is Unicode normalization to NFC: a precomposed character (e.g. the
    single codepoint "u"+combining-diaeresis vs the single codepoint "ü")
    is a harmless input-method/transport difference, not a language
    mistake, and both sides of a comparison should already be NFC in
    practice -- this only protects against the rare case where they
    aren't. Every other kind of difference -- case, whitespace, articles,
    punctuation -- still fails exactly as before. Transport controls are
    parsed outside this function.
    """
    return unicodedata.normalize('NFC', str(answer)) == unicodedata.normalize('NFC', str(word_text))



def mask_sentence(sentence, score):
    """Mask only learnable letters/digits while preserving text structure.

    Whitespace and punctuation are never replaced by underscores: they remain
    literal separators in visible, faded, and fully masked states.  This keeps
    sentences readable while exact answer checking still requires the learner to
    type every space and punctuation mark correctly.
    """
    sentence = str(sentence)
    if score <= 0:
        return sentence
    visible_ratio = 0.0 if score >= 8 else max(0.15, 1.0 - (float(score) / 8.0))
    positions = [i for i, ch in enumerate(sentence) if ch.isalnum()]
    if not positions:
        return sentence
    num_visible = 0 if visible_ratio == 0 else max(1, int(len(positions) * visible_ratio))
    visible_indices = set(random.sample(positions, num_visible))
    return ''.join(
        ch if (not ch.isalnum() or i in visible_indices) else '_'
        for i, ch in enumerate(sentence)
    )


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


SPEECH_RATE = 128  # words per minute -- fixed, matches the pre-generated audio's rate


def speak(text, lang=None):
    """Pipes text to the macOS 'say' command, blocking until speech finishes,
    using a voice matching lang's locale if one is installed. Rate is fixed
    at SPEECH_RATE, matching the pre-generated bundled audio, so live speech
    (personal/custom lists only) sounds consistent with it."""
    if not tts_available():
        return False
    text = str(text).strip()
    if not text:
        return False
    if len(text) > 2_000:
        raise ValueError('Speech text exceeds the 2000-character limit.')
    cmd = ['say', '-r', str(SPEECH_RATE)]
    if lang:
        voice = voice_for_language(lang)
        if voice:
            cmd += ['-v', voice]
    cmd.append(text)
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=min(60, max(5, len(text) * 0.3)))
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return True


def sanitize_name(name, label):
    """Validates a user/language name for safe use in table and file names."""
    name = name.lower()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid {label} '{name}': only lowercase letters, digits, underscores, hyphens, periods, and exclamation marks are allowed."
        )
    return name


def read_word_list(path):
    """Read and validate one material file without changing its shape."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Word list not found: {path}")
    with open(path, encoding='utf-8') as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError(f"Invalid word-list schema in {path}: expected an object.")
    if not isinstance(data.get('metadata'), dict):
        raise ValueError(f"Invalid word-list schema in {path}: metadata must be an object.")
    if not isinstance(data.get('items'), list):
        raise ValueError(f"Invalid word-list schema in {path}: items must be an array.")
    return data


def validate_word_list_items(items, path='<word list>', require_explicit_ids=False):
    """Validate stable IDs and practice fields before material is persisted."""
    if not isinstance(items, list):
        raise ValueError(f"Invalid word-list schema in {path}: items must be an array.")
    seen_ids = set()
    normalized = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Invalid item {index} in {path}: expected an object.")
        word = str(item.get('word', item.get('text', '')))
        if not word.strip():
            raise ValueError(f"Invalid item {index} in {path}: missing word.")
        content_id = str(item.get('id', '')).strip()
        if not content_id:
            if require_explicit_ids:
                raise ValueError(f"Invalid item {index} in {path}: missing stable id.")
            # Generated IDs are anchored to stable source coordinates rather than
            # editable JSON content.  Definition/example/frequency edits therefore do
            # not silently create a second learner-progress identity.
            absolute_path = os.path.abspath(path)
            try:
                source_key = os.path.relpath(absolute_path, os.path.abspath(WORD_LISTS_DIR))
            except ValueError:
                source_key = os.path.basename(absolute_path)
            source_key = os.path.normcase(source_key).replace(os.sep, '/')
            digest = hashlib.sha256(f"{source_key}:{index}:{word}".encode('utf-8')).hexdigest()[:24]
            content_id = f'legacy-{digest}'
        if content_id in seen_ids:
            raise ValueError(f"Invalid word list in {path}: duplicate id '{content_id}'.")
        frequency = normalize_word_frequency(item.get('word_frequency', item.get('frequency', 0)))
        if frequency is None:
            raise ValueError(f"Invalid item {index} in {path}: word_frequency must be a non-negative integer.")
        record = dict(item)
        record['id'] = content_id
        record['word'] = word
        if 'word_frequency' in record or 'frequency' in record:
            record['word_frequency'] = frequency
            record.pop('frequency', None)
        seen_ids.add(content_id)
        normalized.append(record)
    return normalized


def write_word_list_atomic(path, data):
    """Persist validated JSON through a same-directory atomic replacement."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix='.tartarus-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as target:
            json.dump(data, target, ensure_ascii=False, indent=2)
            target.write('\n')
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise


# --- Database Helpers ---
def get_connection():
    """Open the configured progress database without implicit schema writes."""
    return sqlite3.connect(DATABASE_FILE)


def ensure_users_table(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        name TEXT PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')


def ensure_user(conn, user):
    ensure_users_table(conn)
    user = sanitize_name(user, 'user')
    cursor = conn.execute('INSERT OR IGNORE INTO users(name) VALUES (?)', (user,))
    if cursor.rowcount:
        log_event('USER_CREATED', user=user)
    return user


def words_table_name(user, lang):
    return f"words_{sanitize_name(user, 'user')}_{sanitize_name(lang, 'language')}"


def sessions_table_name(user):
    return f"sessions_{sanitize_name(user, 'user')}"


MASTERY_EVENT_TYPES = ('mastered', 'box10')
MASTERY_EVENT_BACKUP_COLUMNS = ['lang', 'word_id', 'event_type', 'mastered_date']


def ensure_mastery_events_table(conn):
    """Create the append-only reporting ledger without altering progress state."""
    conn.execute('''CREATE TABLE IF NOT EXISTS mastery_events (
        id INTEGER PRIMARY KEY,
        user TEXT NOT NULL,
        lang TEXT NOT NULL,
        word_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('mastered', 'box10')),
        mastered_date TEXT NOT NULL,
        UNIQUE(user, lang, word_id, event_type)
    )''')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_mastery_events_user_lang_type_date '
        'ON mastery_events(user, lang, event_type, mastered_date)'
    )


def ensure_pending_drills_table(conn):
    """Create the durable mandatory-drill obligation table.

    A drill in progress is real learner debt -- it must survive a browser
    refresh, a server restart, or a crash, not just live in the in-memory
    session dict."""
    conn.execute('''CREATE TABLE IF NOT EXISTS pending_drills (
        user TEXT NOT NULL,
        lang TEXT NOT NULL,
        word_id INTEGER NOT NULL,
        target INTEGER NOT NULL,
        correct_in_a_row INTEGER NOT NULL DEFAULT 0,
        context TEXT NOT NULL,
        mode TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user, lang, word_id)
    )''')


def ensure_practice_bucket_table(conn):
    """Create the anti-repeat "bag of tiles" table for the supplementary,
    non-scoring practice tracks (Encoding Practice, Reading/Listening
    Retrieval).

    A row's presence means "not yet served this cycle" for that
    (user, lang, track). Selection draws from and removes rows here; when
    empty, it's refilled from the track's current eligible set. This is
    deliberately separate from pending_drills, which is reserved for the
    single durable, cross-restart scoring-drill obligation."""
    conn.execute('''CREATE TABLE IF NOT EXISTS practice_bucket (
        user TEXT NOT NULL,
        lang TEXT NOT NULL,
        track TEXT NOT NULL,
        word_id INTEGER NOT NULL,
        sequence INTEGER,
        PRIMARY KEY (user, lang, track, word_id)
    )''')


def get_pending_drill(user, lang):
    """The durable drill obligation for this (user, lang), if any. Only one
    can be active at a time -- a session processes one question at a time."""
    conn = get_connection()
    try:
        ensure_pending_drills_table(conn)
        row = conn.execute(
            'SELECT word_id,target,correct_in_a_row,context,mode,created_at '
            'FROM pending_drills WHERE user=? AND lang=?',
            (sanitize_name(user, 'user'), sanitize_name(lang, 'language')),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        'word_id': row[0], 'target': row[1], 'correct_in_a_row': row[2],
        'context': row[3], 'mode': row[4], 'created_at': row[5],
    }


def start_pending_drill(conn, user, lang, word_id, target, context, mode, today=None):
    """Record a new mandatory-drill obligation. Replaces any stale row for
    the same word (there should never be one -- a word can't fail twice
    before its first drill resolves -- but idempotent is safer than not)."""
    ensure_pending_drills_table(conn)
    conn.execute(
        'INSERT OR REPLACE INTO pending_drills'
        '(user,lang,word_id,target,correct_in_a_row,context,mode,created_at) '
        'VALUES(?,?,?,?,0,?,?,?)',
        (
            sanitize_name(user, 'user'), sanitize_name(lang, 'language'), word_id,
            target, context, mode, today or date.today().isoformat(),
        ),
    )


def update_pending_drill_progress(conn, user, lang, word_id, correct_in_a_row, target=None):
    """Keep the persisted streak current after every attempt, not just
    failures, so a resumed drill after a crash starts from the real streak
    rather than wherever it stood when the drill began."""
    if target is None:
        conn.execute(
            'UPDATE pending_drills SET correct_in_a_row=? WHERE user=? AND lang=? AND word_id=?',
            (correct_in_a_row, sanitize_name(user, 'user'), sanitize_name(lang, 'language'), word_id),
        )
    else:
        conn.execute(
            'UPDATE pending_drills SET correct_in_a_row=?,target=? WHERE user=? AND lang=? AND word_id=?',
            (correct_in_a_row, target, sanitize_name(user, 'user'), sanitize_name(lang, 'language'), word_id),
        )


def clear_pending_drill(conn, user, lang, word_id):
    conn.execute(
        'DELETE FROM pending_drills WHERE user=? AND lang=? AND word_id=?',
        (sanitize_name(user, 'user'), sanitize_name(lang, 'language'), word_id),
    )


def record_mastery_event(conn, user, lang, word_id, event_type, event_date):
    """Append one transition event; retries and repeated answers stay idempotent."""
    if event_type not in MASTERY_EVENT_TYPES:
        raise ValueError(f'Unsupported mastery event type: {event_type}')
    ensure_mastery_events_table(conn)
    conn.execute(
        'INSERT OR IGNORE INTO mastery_events(user,lang,word_id,event_type,mastered_date) '
        'VALUES(?,?,?,?,?)',
        (sanitize_name(user, 'user'), sanitize_name(lang, 'language'), int(word_id), event_type, str(event_date)[:10]),
    )

SCHEMA_VERSION = 7

# v6 -> v7 rename: the Consolidation Track (formerly "Gauntlet") stage/mode
# vocabulary was renamed to neuroplasticity terminology, in code and in any
# already-persisted mode-key strings. Applied to sessions_<user>.mode and
# pending_drills.mode/context during migration.
MODE_RENAME_V7 = {
    'forging': 'encoding',
    'crucible': 'cued_recall',
    'shadows': 'effortful_retrieval',
    'depths': 'free_recall',
    'void': 'reconsolidation',
    'ascension': 'automaticity',
    'maintenance': 'spaced_maintenance',
    'tartarus': 'consolidation',
}


WORD_TABLE_COLUMNS = [
    'id', 'content_id', 'score', 'last_practiced', 'last_tartarus_completed',
    'active', 'times_practiced', 'times_correct', 'times_incorrect',
    'times_drilled', 'times_mastered', 'leitner_box', 'leitner_last_reviewed',
    'consolidation_step',
]


def table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone() is not None


def word_table_schema(table):
    return f"""
        CREATE TABLE IF NOT EXISTS "{table}" (
            id INTEGER PRIMARY KEY,
            content_id TEXT NOT NULL UNIQUE,
            score REAL NOT NULL DEFAULT 0.0,
            last_practiced TEXT,
            last_tartarus_completed TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            times_practiced INTEGER NOT NULL DEFAULT 0,
            times_correct INTEGER NOT NULL DEFAULT 0,
            times_incorrect INTEGER NOT NULL DEFAULT 0,
            times_drilled INTEGER NOT NULL DEFAULT 0,
            times_mastered INTEGER NOT NULL DEFAULT 0,
            leitner_box INTEGER,
            leitner_last_reviewed TEXT,
            consolidation_step INTEGER NOT NULL DEFAULT 0
        )
    """


def _word_table_columns(conn, table):
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _create_v4_word_table(conn, table):
    conn.execute(word_table_schema(table))


def _copy_word_table_to_v4(conn, source, target):
    columns = set(_word_table_columns(conn, source))
    if not columns:
        raise ValueError(f'Cannot migrate missing table {source}.')
    if 'content_id' in columns:
        content_expr = 'content_id'
    elif 'id' in columns:
        content_expr = "'legacy:' || id"
    else:
        raise ValueError(f'Cannot migrate {source}: no content identity column.')

    def col(name, default='NULL'):
        return name if name in columns else default

    id_expr = col('id', 'NULL')
    score = col('score', '0.0')
    last = col('last_practiced')
    box = col('leitner_box')
    tartarus = col('last_tartarus_completed', last)
    leitner_last = col(
        'leitner_last_reviewed',
        f"CASE WHEN COALESCE({score},0) >= 9.0 AND {box} IS NOT NULL THEN {last} ELSE NULL END",
    )
    # 'gauntlet_completed_day' is this column's pre-rename name in any table
    # that predates the Consolidation Track rename (schema v7); read from
    # whichever name the source table actually has.
    consolidation_step_expr = (
        'gauntlet_completed_day' if 'gauntlet_completed_day' in columns
        else col('consolidation_step', '0')
    )
    select_exprs = [
        id_expr, content_expr, score, last, tartarus, col('active', '1'),
        col('times_practiced', '0'), col('times_correct', '0'),
        col('times_incorrect', '0'), col('times_drilled', '0'),
        col('times_mastered', '0'), box, leitner_last,
        # Real values are backfilled from mastery_events immediately after
        # this rebuild (see migrate_database); this copy just carries
        # forward whatever a table already has, or 0 for a brand-new column.
        consolidation_step_expr,
    ]
    quoted = ', '.join(f'"{c}"' for c in WORD_TABLE_COLUMNS)
    conn.execute(
        f'INSERT INTO "{target}" ({quoted}) SELECT {", ".join(select_exprs)} FROM "{source}"'
    )


def _audit_word_table(conn, table):
    """Return a preservation manifest using only columns present in ``table``."""
    columns = set(_word_table_columns(conn, table))
    def sum_expr(name):
        return f'COALESCE(SUM({name}),0)' if name in columns else '0'
    distinct = 'COUNT(DISTINCT content_id)' if 'content_id' in columns else 'COUNT(*)'
    row = conn.execute(
        f'SELECT COUNT(*), {distinct}, {sum_expr("score")}, {sum_expr("times_practiced")}, '
        f'{sum_expr("times_correct")}, {sum_expr("times_incorrect")}, {sum_expr("times_drilled")}, '
        f'{sum_expr("times_mastered")}, {sum_expr("leitner_box")} FROM "{table}"'
    ).fetchone()
    return tuple(row)


def _preserved_word_rows(conn, table):
    """Snapshot values that v4 promises to preserve exactly, keyed by row id."""
    columns = set(_word_table_columns(conn, table))
    names = [name for name in (
        'id','content_id','score','last_practiced','active','times_practiced',
        'times_correct','times_incorrect','times_drilled','times_mastered','leitner_box'
    ) if name in columns]
    if not names:
        return []
    quoted = ','.join(f'"{name}"' for name in names)
    return (tuple(names), conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY id').fetchall())


def verified_database_backup(database_file=None, label='snapshot'):
    """Create and fsync a SQLite-consistent backup after checking both databases."""
    db_path=os.path.abspath(database_file or DATABASE_FILE)
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    safe_label=re.sub(r'[^a-z0-9-]+','-',str(label).lower()).strip('-') or 'snapshot'
    stamp=datetime.now().strftime('%Y%m%d%H%M%S%f')
    backup_path=f'{db_path}.pre-{safe_label}.{stamp}.sqlite'
    source=sqlite3.connect(f'file:{db_path}?mode=ro',uri=True)
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Database integrity check failed before backup.')
        target=sqlite3.connect(backup_path)
        try:
            source.backup(target); target.commit()
        finally: target.close()
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(backup_path)
        raise
    finally: source.close()
    with open(backup_path,'rb+') as handle:
        handle.flush(); os.fsync(handle.fileno())
    check=sqlite3.connect(f'file:{backup_path}?mode=ro',uri=True)
    try:
        if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Backup integrity check failed.')
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(backup_path)
        raise
    finally: check.close()
    return backup_path

def _database_audit_manifest(conn):
    result = {}
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'words_%' ORDER BY name"
    ):
        if table.endswith('_legacy') or '__v4_' in table:
            raise ValueError(f'Unexpected migration scratch table: {table}')
        result[table] = _audit_word_table(conn, table)
    result['mastery_event_rows'] = (
        conn.execute('SELECT COUNT(*) FROM mastery_events').fetchone()[0]
        if table_exists(conn, 'mastery_events') else 0
    )
    result['session_rows'] = sum(
        conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sessions_%'"
        )
    )
    return result


def migrate_database(database_file=None, *, create_backup=True, fail_after_tables=None):
    """Atomically migrate progress storage to the current schema.

    A SQLite-consistent verified backup is created before mutation unless
    ``create_backup`` is false. The whole database migration is one transaction.
    ``fail_after_tables`` exists only for rollback tests.
    """
    db_path = database_file or DATABASE_FILE
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    if not os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        ensure_mastery_events_table(conn)
        ensure_pending_drills_table(conn)
        ensure_practice_bucket_table(conn)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit(); conn.close()
        return None

    source = sqlite3.connect(db_path)
    backup_path = None
    try:
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed before migration.')
        version = source.execute('PRAGMA user_version').fetchone()[0]
        tables = [r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'words_%' ORDER BY name"
        )]
        session_tables = [r[0] for r in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sessions_%' ORDER BY name"
        )]
        already_current = (
            version >= SCHEMA_VERSION
            and not table_exists(source, 'dataset_progress')
            and table_exists(source, 'pending_drills')
            and table_exists(source, 'practice_bucket')
            and all(_word_table_columns(source, table) == WORD_TABLE_COLUMNS for table in tables)
            and all(
                {'mode', 'stage'} <= {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
                for table in session_tables
            )
        )
        if already_current:
            return None
        before = _database_audit_manifest(source)
        if create_backup:
            backup_path = verified_database_backup(db_path, f'v{SCHEMA_VERSION}')

        source.execute('BEGIN IMMEDIATE')
        ensure_mastery_events_table(source)
        migrated = 0
        for table in tables:
            columns = _word_table_columns(source, table)
            if columns == WORD_TABLE_COLUMNS:
                continue
            scratch = f'{table}__v4_{uuid.uuid4().hex[:8]}'
            _create_v4_word_table(source, scratch)
            src_count = source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            preserved = _preserved_word_rows(source, table)
            _copy_word_table_to_v4(source, table, scratch)
            dst_count = source.execute(f'SELECT COUNT(*) FROM "{scratch}"').fetchone()[0]
            if src_count != dst_count:
                raise ValueError(f'Row-count mismatch while migrating {table}.')
            if preserved:
                names, expected_rows = preserved
                quoted = ','.join(f'"{name}"' for name in names)
                actual_rows = source.execute(f'SELECT {quoted} FROM "{scratch}" ORDER BY id').fetchall()
                if actual_rows != expected_rows:
                    raise ValueError(f'Preserved row values changed while migrating {table}.')
            source.execute(f'DROP TABLE "{table}"')
            source.execute(f'ALTER TABLE "{scratch}" RENAME TO "{table}"')
            migrated += 1
            if fail_after_tables is not None and migrated >= fail_after_tables:
                raise RuntimeError('Injected migration failure')

        if table_exists(source, 'dataset_progress'):
            source.execute('DROP TABLE dataset_progress')

        ensure_pending_drills_table(source)
        ensure_practice_bucket_table(source)

        for table in session_tables:
            existing_columns = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
            if 'mode' not in existing_columns:
                source.execute(f'ALTER TABLE "{table}" ADD COLUMN mode TEXT')
            if 'stage' not in existing_columns:
                source.execute(f'ALTER TABLE "{table}" ADD COLUMN stage INTEGER')

        # v7: rewrite any already-persisted mode-key strings (session rows,
        # durable drill obligations) to the renamed Consolidation Track
        # vocabulary, so old and new rows never disagree on what the same
        # mode is called.
        for old_mode, new_mode in MODE_RENAME_V7.items():
            for table in session_tables:
                source.execute(
                    f'UPDATE "{table}" SET mode=? WHERE mode=?', (new_mode, old_mode)
                )
            if table_exists(source, 'pending_drills'):
                source.execute(
                    'UPDATE pending_drills SET mode=? WHERE mode=?', (new_mode, old_mode)
                )
                source.execute(
                    'UPDATE pending_drills SET context=? WHERE context=?', (new_mode, old_mode)
                )

        # Backfill consolidation_step for every already-mastered word from
        # real completion evidence (last_tartarus_completed), not a guess:
        # completed steps = calendar days between mastery and the last
        # recorded reinforcement completion, clamped to the 0-10 track.
        # mastery_events already carries (user, lang) per row, so grouping by
        # it -- rather than trying to reverse-parse a words_<user>_<lang>
        # table name, which is not reliably reversible when either contains
        # an underscore -- is what correctly scopes each backfill to the
        # right table.
        pairs = source.execute(
            "SELECT DISTINCT user, lang FROM mastery_events WHERE event_type='mastered'"
        ).fetchall()
        for user, lang in pairs:
            table = words_table_name(user, lang)
            if table not in tables:
                continue
            rows = source.execute(
                f'SELECT w.id, w.last_tartarus_completed, e.mastered_date '
                f'FROM "{table}" AS w JOIN mastery_events AS e '
                "ON e.user=? AND e.lang=? AND e.word_id=w.id AND e.event_type='mastered' "
                'WHERE w.score>=9.0',
                (user, lang),
            ).fetchall()
            updates = []
            for word_id, last_completed, mastered_date in rows:
                if not last_completed:
                    continue
                days = (
                    date.fromisoformat(str(last_completed)[:10])
                    - date.fromisoformat(str(mastered_date)[:10])
                ).days
                updates.append((min(10, max(0, days)), word_id))
            if updates:
                source.executemany(
                    f'UPDATE "{table}" SET consolidation_step=? WHERE id=?', updates
                )

        source.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        after = _database_audit_manifest(source)
        if after != before:
            raise ValueError('Progress audit mismatch after database migration.')
        source.commit()
        if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed after migration.')
        return backup_path
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()


def initialize_database(*, create_backup=True):
    """Initialize/migrate the configured progress DB at an explicit boundary."""
    migrate_database(DATABASE_FILE, create_backup=create_backup)
    conn = get_connection()
    try:
        ensure_users_table(conn)
        ensure_mastery_events_table(conn)
        ensure_pending_drills_table(conn)
        ensure_practice_bucket_table(conn)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit()
    finally:
        conn.close()


def ensure_word_table(conn, user, lang):
    """Create a fresh word table or verify an already-migrated one."""
    ensure_mastery_events_table(conn)
    table = words_table_name(user, lang)
    if not table_exists(conn, table):
        _create_v4_word_table(conn, table)
    columns = _word_table_columns(conn, table)
    if columns != WORD_TABLE_COLUMNS:
        raise RuntimeError(
            f'Progress table {table} is not current. Run initialize_database() before use.'
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
            drilled_count INTEGER NOT NULL DEFAULT 0,
            mode TEXT,
            stage INTEGER
        )
    ''')
    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    if 'mode' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN mode TEXT')
    if 'stage' not in columns:
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN stage INTEGER')
    return table


# ---------------------------------------------------------------------------
# Consolidation Track (10-Day Descent) constants and helpers
# ---------------------------------------------------------------------------

# (stage, day_min, day_max, stage_name, session_mode)
CONSOLIDATION_STAGE_MAP = [
    (0,  0,  0,  'Encoding',             'encoding'),
    (1,  1,  2,  'Cued Recall',          'cued_recall'),
    (2,  3,  4,  'Effortful Retrieval',  'effortful_retrieval'),
    (3,  5,  6,  'Free Recall',          'free_recall'),
    (4,  7,  8,  'Reconsolidation',      'reconsolidation'),
    (5,  9,  10, 'Automaticity',         'automaticity'),
]

CONSOLIDATION_MAX_DAY = 10
CONSOLIDATION_COMPLETE_DAY = 11
# Day 11 is a terminal Tartarus state; Spaced Maintenance may continue.


def consolidation_stage_for_day(day):
    """Return (stage_num, stage_name, session_mode) for Consolidation Track day 0..11."""
    day = int(day or 0)
    for stage, day_min, day_max, name, mode in CONSOLIDATION_STAGE_MAP:
        if day_min <= day <= day_max:
            return stage, name, mode
    if day >= CONSOLIDATION_COMPLETE_DAY:
        return 5, 'Automaticity', 'complete'
    return 0, 'Encoding', 'encoding'



def consolidation_next_day(completed_day):
    """The reinforcement day a word is currently working on: one past the
    last step it actually completed, clamped to the 10-day track.

    Purely a function of persisted progress -- elapsed calendar time can
    never advance or skip this on its own. Missing any number of days just
    means the word waits at its last completed step until the learner
    returns; it does not lose credit and does not jump ahead."""
    return min(max(int(completed_day or 0) + 1, 1), CONSOLIDATION_MAX_DAY)


def _reinforcement_rows(conn, user, lang, today, *, due_only=False):
    """Return score-9 rows still inside their independent 10-day tracks,
    keyed off each word's own persisted consolidation_step."""
    table = words_table_name(user, lang)
    if not table_exists(conn, table):
        return []
    today = str(today)[:10]
    due_clause = (
        'AND (last_tartarus_completed IS NULL OR last_tartarus_completed < ?)'
        if due_only else ''
    )
    params = [today] if due_only else []
    rows = conn.execute(
        f'SELECT id,content_id,score,leitner_box,last_tartarus_completed,consolidation_step '
        f'FROM "{table}" '
        f'WHERE active=1 AND score>=9.0 AND consolidation_step<{CONSOLIDATION_MAX_DAY} '
        f'{due_clause} ORDER BY id',
        params,
    ).fetchall()
    result = []
    for row_id, content_id, score, box, last_completed, completed_day in rows:
        day = consolidation_next_day(completed_day)
        stage, stage_name, mode = consolidation_stage_for_day(day)
        result.append({
            'id': row_id,
            'content_id': content_id,
            'score': score,
            'leitner_box': box,
            'last_tartarus_completed': last_completed,
            'completed_day': int(completed_day or 0),
            'day': day,
            'stage': stage,
            'stage_name': stage_name,
            'mode': mode,
        })
    return result


def _consolidation_tasks_remaining(conn, user, lang, practice_date):
    """Count due reinforcement tasks across every active mastery cohort."""
    return len(_reinforcement_rows(
        conn, user, lang, practice_date, due_only=True
    ))


def get_consolidation_tasks_remaining(user, lang, practice_date=None):
    """Return due per-word reinforcement tasks for one calendar date."""
    conn = get_connection()
    try:
        return _consolidation_tasks_remaining(
            conn, user, lang, practice_date or date.today().isoformat()
        )
    finally:
        conn.close()


def get_words_for_consolidation_stage(user, lang, stage, num_words=None, today=None):
    """Select Encoding work; reinforcement is selected per word separately."""
    if int(stage or 0) != 0:
        raise ValueError('Reinforcement stages are selected per word.')
    num_words = MAX_QUESTIONS if num_words is None else num_words
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    try:
        if not table_exists(conn, table):
            raise ValueError('No progress table exists for this list.')
        rows = conn.execute(
            f'SELECT id,content_id,score,leitner_box FROM "{table}" '
            'WHERE active=1 AND score < 9.0'
        ).fetchall()
    finally:
        conn.close()
    candidates = []
    positions = {}
    for row_id, content_id, score, box in rows:
        item = material.get(content_id)
        if item:
            positions[row_id] = item['position']
            candidates.append((
                row_id, item['word'], item['definition'], score, box,
                item['word_frequency'],
            ))
    if not candidates:
        raise ValueError('Encoding is complete for this list.')
    candidates.sort(key=lambda row: (-row[3], positions[row[0]], row[0]))
    selected = candidates[:num_words]
    ordered = []
    index = 0
    while index < len(selected):
        score = selected[index][3]
        end = index + 1
        while end < len(selected) and selected[end][3] == score:
            end += 1
        group = selected[index:end]
        random.shuffle(group)
        ordered.extend(group)
        index = end
    return ordered


def get_words_for_reinforcement(user, lang, num_words=None, today=None, stage=None):
    """Select due words from exactly one Consolidation Track stage.

    A session must never mix stages -- Cued Recall, Effortful Retrieval, Free Recall, Reconsolidation, and
    Automaticity each carry a different masking/audio/timer presentation, and
    switching between them mid-session is a jarring context switch for the
    learner. If the chosen stage has fewer than num_words due, the session
    is simply smaller; it is never padded from another stage or track.

    ``stage``, when given, forces that specific stage's pool -- this is how
    select_practice_words() applies its own cross-pool fairness decision
    (which due pool, of every stage and Leitner, has been waiting longest)
    rather than letting this function re-derive a possibly different
    answer. Called without a hint (e.g. directly, or by tests), the
    earliest-numbered due stage wins."""
    num_words = MAX_QUESTIONS if num_words is None else num_words
    today = today or date.today().isoformat()
    material = {
        item['content_id']: item
        for item in load_practice_items(word_list_path(user, lang))
    }
    conn = get_connection()
    try:
        rows = _reinforcement_rows(conn, user, lang, today, due_only=True)
    finally:
        conn.close()
    by_stage = {}
    for row in rows:
        item = material.get(row['content_id'])
        if not item:
            continue
        by_stage.setdefault(row['stage'], []).append((row, item))
    if not by_stage:
        return []
    chosen_stage = stage if stage in by_stage else min(by_stage)
    candidates = [
        (
            row['id'], item['word'], item['definition'], row['score'],
            row['leitner_box'], item['word_frequency'], row['mode'],
            row['stage'], row['stage_name'], row['day'],
        )
        for row, item in by_stage[chosen_stage]
    ]
    random.shuffle(candidates)
    return candidates[:num_words]


def _next_practice_pool(conn, user, lang, today):
    """Return (winner, due_rows, stage_counts, due_maintenance) for whichever
    due pool -- one Consolidation Track stage, or Spaced Maintenance -- has been
    waiting longest. ``winner`` is a stage number (1-5), 6 for maintenance,
    or None if nothing is due.

    This is the single definition of scheduling priority: both
    consolidation_state_breakdown() (for reporting what's available) and
    select_practice_words() (for actually building a session) call it,
    rather than each keeping its own copy that could quietly drift apart
    (P7).
    """
    due_rows = _reinforcement_rows(conn, user, lang, today, due_only=True)
    maintenance_due_since = _maintenance_due_since(conn, user, lang, today)

    stage_due_since = {}
    stage_counts = {}
    for row in due_rows:
        stage_counts[row['stage']] = stage_counts.get(row['stage'], 0) + 1
        last_completed = row['last_tartarus_completed']
        due_since = (
            date.fromisoformat(str(last_completed)[:10]) + timedelta(days=1)
            if last_completed else date.min
        )
        current = stage_due_since.get(row['stage'])
        if current is None or due_since < current:
            stage_due_since[row['stage']] = due_since

    pools = [(due_since, stage) for stage, due_since in stage_due_since.items()]
    due_maintenance = 0
    if maintenance_due_since is not None:
        due_maintenance = len(maintenance_ready_words(user, lang, today=today))
        pools.append((maintenance_due_since, 6))  # sorts after every real stage (1-5) on a tie
    pools.sort()

    winner = pools[0][1] if pools else None
    return winner, due_rows, stage_counts, due_maintenance


def consolidation_state_breakdown(user, lang, today=None, conn=None):
    """Return cohort counts without creating or advancing mutable state."""
    today = str(today or date.today().isoformat())[:10]
    close = conn is None
    if close:
        conn = get_connection()
    try:
        table = words_table_name(user, lang)
        if not table_exists(conn, table):
            total = encoding = mastered = 0
        else:
            total, encoding, mastered = conn.execute(
                f'SELECT COUNT(*),'
                'SUM(CASE WHEN score<9.0 THEN 1 ELSE 0 END),'
                'SUM(CASE WHEN score>=9.0 THEN 1 ELSE 0 END) '
                f'FROM "{table}" WHERE active=1'
            ).fetchone()
            encoding = int(encoding or 0)
            mastered = int(mastered or 0)
        track_rows = _reinforcement_rows(conn, user, lang, today)
        winner, due_rows, due_stage_counts, due_maintenance = _next_practice_pool(
            conn, user, lang, today
        )
        due = len(due_rows)
        stage_counts = {stage: 0 for stage in range(1, 6)}
        for row in track_rows:
            stage_counts[row['stage']] += 1
        stages = []
        for stage, day_min, day_max, name, mode in CONSOLIDATION_STAGE_MAP[1:]:
            stages.append({
                'stage': stage,
                'name': name,
                'mode': mode,
                'days': f'{day_min}-{day_max}',
                'count': stage_counts[stage],
            })
        reinforcement = len(track_rows)
        long_term = max(0, mastered - reinforcement)
        # available_tasks always equals the size of whichever pool
        # select_practice_words() would actually serve next -- the due
        # Consolidation Track stage that's waited longest, or due Spaced Maintenance,
        # or Encoding once nothing is due (P7).
        if winner == 6:
            available = due_maintenance
        elif winner is not None:
            available = due_stage_counts.get(winner, 0)
        else:
            available = encoding
        complete = bool(total and encoding == 0 and reinforcement == 0)
        return {
            'total_tasks': int(total or 0),
            'encoding': encoding,
            'mastered_total': mastered,
            'reinforcement_total': reinforcement,
            'reinforcement_stages': stages,
            'long_term_review': long_term,
            'due_reinforcement': due,
            'due_maintenance': due_maintenance,
            'available_tasks': available,
            'complete': complete,
            'locked_today': bool(
                encoding == 0 and reinforcement > 0 and due == 0
            ),
        }
    finally:
        if close:
            conn.close()


def maintenance_ready_words(user, lang, num_words=None, today=None):
    """Return score-9 items ready for Spaced Maintenance, without mutation."""
    num_words = MAX_QUESTIONS if num_words is None else num_words
    today_date = date.fromisoformat(today or date.today().isoformat())
    wpath = word_list_path(user, lang)
    material = {item['content_id']: item for item in load_practice_items(wpath)}
    table = words_table_name(user, lang)
    conn = get_connection()
    try:
        if not table_exists(conn, table):
            return []
        rows = conn.execute(
            f'SELECT id,content_id,score,leitner_box,leitner_last_reviewed FROM "{table}" '
            f'WHERE active=1 AND score >= 9.0 AND leitner_box IS NOT NULL ORDER BY id'
        ).fetchall()
    finally:
        conn.close()
    ready = []
    for row_id, content_id, score, box, last_reviewed in rows:
        box = int(box or 1)
        interval = LEITNER_INTERVALS.get(box, 10)
        is_ready = last_reviewed is None
        if last_reviewed:
            reviewed = date.fromisoformat(str(last_reviewed)[:10])
            is_ready = (today_date - reviewed).days >= interval
        if is_ready and content_id in material:
            item = material[content_id]
            ready.append((
                row_id, item['word'], item['definition'], score, box,
                item['word_frequency'],
            ))
    # Box 1 (1-day interval, the least stable memories) goes first, through
    # to the highest box due -- a maintenance session should always work
    # down from the most fragile items, not whatever file order they
    # happen to sit in. Stable sort keeps file order as the tiebreaker
    # within the same box.
    ready.sort(key=lambda row: row[4])
    return ready[:num_words]


def maintenance_next_date(leitner_box, leitner_last_reviewed):
    if not leitner_box or not leitner_last_reviewed:
        return None
    reviewed = date.fromisoformat(str(leitner_last_reviewed)[:10])
    return (
        reviewed + timedelta(days=LEITNER_INTERVALS.get(int(leitner_box), 10))
    ).isoformat()


def _with_stage(rows, mode, stage, stage_name, day):
    return [
        (*row, mode, stage, stage_name, day)
        for row in rows
    ]


def _maintenance_due_since(conn, user, lang, today):
    """Earliest date any currently-due Leitner item became due, or None if
    nothing is due. Mirrors maintenance_ready_words()'s own readiness rule
    without changing that function's return shape or callers."""
    table = words_table_name(user, lang)
    if not table_exists(conn, table):
        return None
    today_date = date.fromisoformat(str(today)[:10])
    rows = conn.execute(
        f'SELECT leitner_box, leitner_last_reviewed FROM "{table}" '
        'WHERE active=1 AND score>=9.0 AND leitner_box IS NOT NULL'
    ).fetchall()
    earliest = None
    for box, last_reviewed in rows:
        interval = LEITNER_INTERVALS.get(int(box or 1), 10)
        if last_reviewed is None:
            due_since = today_date
        else:
            due_since = date.fromisoformat(str(last_reviewed)[:10]) + timedelta(days=interval)
            if due_since > today_date:
                continue
        if earliest is None or due_since < earliest:
            earliest = due_since
    return earliest


def select_practice_words(user, lang, today=None):
    """Choose the next single-mode session: whichever due pool -- one
    Consolidation Track stage, or Spaced Maintenance -- has been waiting longest,
    then Encoding once nothing is due.

    Reinforcement and Spaced Maintenance are both already-mastered review,
    never new material, so between them the order is a fairness question,
    not a track-priority one: whichever pool has been due the longest goes
    next, so a large reinforcement backlog can never starve overdue Leitner
    indefinitely (or the reverse). Ties break by stage ascending, then
    maintenance last -- reinforcement's scaffolded presentation still
    generally warms a session up before Leitner's unscaffolded pure recall
    when both became due on the same date. Either one still always
    outranks starting brand-new Encoding material.
    """
    today = today or date.today().isoformat()
    state = consolidation_state_breakdown(user, lang, today)

    conn = get_connection()
    try:
        winner, _due_rows, _stage_counts, _due_maintenance = _next_practice_pool(
            conn, user, lang, today
        )
    finally:
        conn.close()

    if winner is not None:
        if winner == 6:
            words = maintenance_ready_words(user, lang, today=today)
            words = _with_stage(words, 'spaced_maintenance', 5, 'Spaced Maintenance', 0)
            return (
                words, 'spaced_maintenance', 'spaced_maintenance', 5,
                'Spaced Maintenance', 0, state,
            )
        words = get_words_for_reinforcement(user, lang, today=today, stage=winner)
        first = words[0]
        return (
            words, 'consolidation', first[6], first[7], first[8], first[9], state,
        )

    if state['encoding']:
        words = _with_stage(
            get_words_for_consolidation_stage(user, lang, 0, today=today),
            'encoding', 0, 'Encoding', 0,
        )
        return words, 'consolidation', 'encoding', 0, 'Encoding', 0, state

    return [], 'consolidation', 'complete', 5, 'Complete', CONSOLIDATION_COMPLETE_DAY, state


PRACTICE_BUCKET_TRACKS = ('encoding_practice', 'retrieval_reading', 'retrieval_listening')


def _bucket_eligible_items(conn, user, lang, track):
    """Return every word-table row eligible for one supplementary practice
    track, joined with its material record. Encoding Practice draws from
    score<9 items, falling back to every active item when none are below
    band 9 (per the confirmed selection rule); Reading/Listening Retrieval
    draw from score>=9 (mastered) items only."""
    table = words_table_name(user, lang)
    if not table_exists(conn, table):
        return []
    material = {
        item['content_id']: item
        for item in load_practice_items(word_list_path(user, lang))
    }
    if track == 'encoding_practice':
        rows = conn.execute(
            f'SELECT id,content_id,score FROM "{table}" WHERE active=1 AND score < 9.0'
        ).fetchall()
        if not rows:
            rows = conn.execute(
                f'SELECT id,content_id,score FROM "{table}" WHERE active=1'
            ).fetchall()
    else:
        rows = conn.execute(
            f'SELECT id,content_id,score FROM "{table}" WHERE active=1 AND score >= 9.0'
        ).fetchall()
    result = []
    for row_id, content_id, score in rows:
        item = material.get(content_id)
        if not item:
            continue
        result.append({
            'id': row_id, 'word': item['word'], 'definition': item['definition'],
            'score': score, 'position': item['position'],
        })
    return result


def select_bucket_words(user, lang, track, num_words=None):
    """Select items for one supplementary, non-scoring practice track
    (Encoding Practice, Reading Retrieval, Listening Retrieval).

    With ``num_words`` left at its default (``None``) -- the case for every
    real session start -- there is no cap: every currently-eligible item is
    drawn, freshly shuffled, every single call. That's what makes these
    tracks "endless": a session runs until either the learner ends it or
    the shuffled set runs out, and no two sessions land in the same order.
    Any pre-existing persisted bucket rows for this user/lang/track are
    cleared first, so a stale partial cycle from an old bounded session can
    never make one of these unlimited draws short-change the eligible set.

    Passing an explicit ``num_words`` instead exercises the legacy bounded
    "bag of tiles" bucket: up to ``num_words`` items are drawn from a
    persisted cycle so the same items never repeat until every
    currently-eligible item has been drawn once, at which point the bucket
    refills from the then-current eligible set and a new cycle begins.
    Kept for callers that want a bounded, non-repeating draw across
    repeated calls.

    This mutates practice_bucket (refill + draw) but never touches a
    word-table's score, leitner_box, or consolidation_step -- these tracks
    are read-only against normal progress state, by design.

    Drawn items are removed from the bucket immediately, at selection time
    -- not deferred to when each question is later answered -- so even an
    abandoned or partially-finished bounded-cycle session still advances
    the cycle instead of re-serving the same unattempted items
    indefinitely.
    """
    if track not in PRACTICE_BUCKET_TRACKS:
        raise ValueError(f'Unknown practice track: {track}')
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    conn = get_connection()
    try:
        ensure_practice_bucket_table(conn)
        eligible = _bucket_eligible_items(conn, user, lang, track)
        eligible_by_id = {item['id']: item for item in eligible}
        if not eligible_by_id:
            conn.execute(
                'DELETE FROM practice_bucket WHERE user=? AND lang=? AND track=?',
                (user, lang, track),
            )
            conn.commit()
            return []
        if num_words is None:
            # Unlimited: every call is a fresh, fully-shuffled pass over
            # everything eligible right now -- no cross-session bucket
            # state to keep in sync, so any leftover rows from a prior
            # bounded cycle are discarded rather than consulted.
            in_fallback = track == 'encoding_practice' and not any(
                item['score'] < 9.0 for item in eligible
            )
            if in_fallback:
                ordered = sorted(eligible, key=lambda item: item['position'])
            else:
                ordered = list(eligible)
                random.shuffle(ordered)
            conn.execute(
                'DELETE FROM practice_bucket WHERE user=? AND lang=? AND track=?',
                (user, lang, track),
            )
            conn.commit()
            return [
                (item['id'], item['word'], item['definition'], item['score'])
                for item in ordered
            ]
        bucket_ids = [
            word_id for (word_id,) in conn.execute(
                'SELECT word_id FROM practice_bucket WHERE user=? AND lang=? AND track=? '
                'ORDER BY sequence, word_id',
                (user, lang, track),
            ).fetchall()
            if word_id in eligible_by_id
        ]
        if not bucket_ids:
            # Refill: a fresh cycle over the current eligible set. Encoding
            # Practice's fallback (no sub-9 items left) draws in file
            # order, per the confirmed selection rule; every other case is
            # shuffled, matching this app's other practice selectors.
            in_fallback = track == 'encoding_practice' and not any(
                item['score'] < 9.0 for item in eligible
            )
            if in_fallback:
                ordered = sorted(eligible, key=lambda item: item['position'])
            else:
                ordered = list(eligible)
                random.shuffle(ordered)
            conn.execute(
                'DELETE FROM practice_bucket WHERE user=? AND lang=? AND track=?',
                (user, lang, track),
            )
            conn.executemany(
                'INSERT INTO practice_bucket(user,lang,track,word_id,sequence) VALUES(?,?,?,?,?)',
                [(user, lang, track, item['id'], i) for i, item in enumerate(ordered)],
            )
            conn.commit()
            bucket_ids = [item['id'] for item in ordered]
        drawn_ids = bucket_ids[:num_words]
        conn.executemany(
            'DELETE FROM practice_bucket WHERE user=? AND lang=? AND track=? AND word_id=?',
            [(user, lang, track, word_id) for word_id in drawn_ids],
        )
        conn.commit()
        return [
            (item['id'], item['word'], item['definition'], item['score'])
            for item in (eligible_by_id[word_id] for word_id in drawn_ids)
        ]
    finally:
        conn.close()


# --- Word List Sync ---
def word_list_path(user, lang):
    """Resolve a personal override or one unambiguous shared material file."""
    user = sanitize_name(user, 'user')
    lang = sanitize_name(lang, 'language')
    user_specific = os.path.join(WORD_LISTS_DIR, f"{user}_{lang}.json")
    if os.path.isfile(user_specific):
        return user_specific

    matches = []
    for root, _, names in os.walk(WORD_LISTS_DIR):
        if f'{lang}.json' in names:
            candidate = os.path.join(root, f'{lang}.json')
            if candidate != user_specific:
                matches.append(candidate)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"Word list '{lang}' was not found.")
    locations = ', '.join(os.path.relpath(candidate, WORD_LISTS_DIR) for candidate in matches)
    raise ValueError(f"Word list id '{lang}' is ambiguous: {locations}")


def audio_relative_stem(json_path):
    """e.g. .../word_lists/german/vocabulary/a1/foo.json -> german/vocabulary/a1/foo

    Shared by the generator and this runtime lookup so the write path and
    the read path can never drift apart."""
    rel = os.path.relpath(json_path, WORD_LISTS_DIR)
    return os.path.splitext(rel)[0]


def audio_part_path(stem, part_number):
    return os.path.join(AUDIO_DIR, f'{stem}.part{part_number}.db')


def bundled_audio_db_paths(stem):
    """Every generated .partN.db for one stem, in part order. Empty for a
    stem with no pre-generated audio (personal overrides, custom lists).
    Parts are always written sequentially with no gaps, so probing in
    order is equivalent to (and simpler than) scanning the directory."""
    paths = []
    part = 1
    while True:
        candidate = audio_part_path(stem, part)
        if not os.path.isfile(candidate):
            break
        paths.append(candidate)
        part += 1
    return paths


def lookup_bundled_audio(user, lang, text):
    """(audio_bytes, content_type) for one word's pre-generated pronunciation,
    or None if this list has no generated audio (personal override, unknown
    list) or this exact text isn't in it. Never raises -- a lookup miss is
    the normal, expected outcome that callers fall back to live speech for."""
    try:
        path = word_list_path(user, lang)
    except (FileNotFoundError, ValueError):
        return None
    for db_path in bundled_audio_db_paths(audio_relative_stem(path)):
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        try:
            row = conn.execute(
                'SELECT audio, content_type FROM audio WHERE text=?', (text,)
            ).fetchone()
        finally:
            conn.close()
        if row:
            return row
    return None


def personal_list_owner(stem, users):
    """Return the longest matching user prefix for ``owner_list`` names."""
    matches = [user for user in users if stem.startswith(f'{user}_')]
    return max(matches, key=len) if matches else None


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
    return value if value >= 0 else 0


def canonical_material_metadata(metadata=None, *, name=None, language=None, kind='vocabulary', level='all'):
    """Return Master Schema metadata using canonical ``kind``/``level`` keys.

    Unknown metadata is preserved, while legacy aliases are normalized on new
    personal files.  This keeps readers backward compatible without writing new
    ``type`` / ``cefr_level`` variants.
    """
    result = dict(metadata or {})
    resolved_kind = str(result.pop('type', result.get('kind', kind)) or kind).lower()
    resolved_level = str(result.pop('cefr_level', result.get('level', level)) or level).lower()
    result['name'] = str(result.get('name') or name or 'Untitled')
    result['language'] = str(result.get('language') or language or 'unknown').lower()
    result['kind'] = 'sentences' if resolved_kind == 'sentences' else 'vocabulary'
    result['level'] = resolved_level
    return result


def sync_word_list(user, lang):
    """Synchronize JSON identities at an explicit mutation boundary."""
    initialize_database(create_backup=False)
    path = word_list_path(user, lang)
    entries = load_practice_items(path)
    conn = get_connection()
    try:
        table = ensure_word_table(conn, user, lang)
        ensure_user(conn, user); ensure_sessions_table(conn, user)
        seen_ids={entry['content_id'] for entry in entries}
        previously_active={cid for (cid,) in conn.execute(f'SELECT content_id FROM "{table}" WHERE active=1')}
        before_count=conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        conn.executemany(
            f'INSERT OR IGNORE INTO "{table}" (content_id) VALUES (?)',
            ((entry['content_id'],) for entry in entries),
        )
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS tartarus_seen_ids (content_id TEXT PRIMARY KEY)')
        conn.execute('DELETE FROM tartarus_seen_ids')
        conn.executemany(
            'INSERT INTO tartarus_seen_ids(content_id) VALUES (?)',
            ((content_id,) for content_id in seen_ids),
        )
        conn.execute(
            f'UPDATE "{table}" SET active=0 WHERE active=1 AND NOT EXISTS '
            f'(SELECT 1 FROM tartarus_seen_ids WHERE tartarus_seen_ids.content_id="{table}".content_id)'
        )
        conn.execute(
            f'UPDATE "{table}" SET active=1 WHERE active=0 AND EXISTS '
            f'(SELECT 1 FROM tartarus_seen_ids WHERE tartarus_seen_ids.content_id="{table}".content_id)'
        )
        added=conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]-before_count
        conn.commit()
    finally:
        conn.close()
    deactivated=len(previously_active-seen_ids)
    if added or deactivated:
        log_event('WORD_LIST_SYNCED', user=user, lang=lang, added=added, deactivated=deactivated, total=len(entries))


def reset_word_list_progress(user, lang):
    """Restart one list while preserving factual session history.

    Scores, completion markers, Leitner state, Consolidation Track step counts, and
    milestone events are cleared.
    """
    table = words_table_name(user, lang)
    conn = get_connection()
    try:
        if not table_exists(conn, table):
            raise ValueError(f"No progress exists yet for '{lang}'.")
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            f'UPDATE "{table}" SET score=0.0, last_practiced=NULL, last_tartarus_completed=NULL, '
            'times_practiced=0, times_correct=0, times_incorrect=0, times_drilled=0, times_mastered=0, '
            'leitner_box=NULL, leitner_last_reviewed=NULL, consolidation_step=0'
        )
        ensure_mastery_events_table(conn)
        conn.execute('DELETE FROM mastery_events WHERE user=? AND lang=?', (user, lang))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_event('PROGRESS_RESET', user=user, lang=lang)


def _user_last_practiced(conn, user_s, word_tables, sessions_table):
    """Most recent date this user actually did anything, across every
    signal that would count as "practiced": any word's last_practiced, or
    a logged session's date. Returns an ISO date string, or None if this
    user has never practiced at all."""
    last = None
    for table in word_tables:
        value = conn.execute(f'SELECT MAX(last_practiced) FROM "{table}"').fetchone()[0]
        if value and (last is None or value > last):
            last = value
    if table_exists(conn, sessions_table):
        value = conn.execute(f'SELECT MAX(session_date) FROM "{sessions_table}"').fetchone()[0]
        if value and (last is None or value > last):
            last = value
    return str(last)[:10] if last else None


def _unparseable_date_columns(conn, table, columns):
    """Column names in `table` holding at least one non-NULL value SQLite's
    own date() function can't parse. date(bad_value, '+1 days') silently
    returns NULL rather than erroring, which would quietly wipe out
    whatever was there -- this is the pre-flight check that turns that
    into a loud failure before anything is touched, instead of a silent
    loss of data."""
    bad = []
    for col in columns:
        count = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL AND date("{col}") IS NULL'
        ).fetchone()[0]
        if count:
            bad.append(col)
    return bad


def _user_max_shifted_date(conn, user_s, word_tables, sessions_table):
    """The latest date held in ANY column this shift would move, for this
    user. The shift distance is computed from this -- not from
    last_practiced -- so that "land everything on today" is provably
    incapable of pushing any single value past today: every shifted value
    is <= this maximum by definition, so adding (today - maximum) to all of
    them leaves every one of them <= today. Returns an ISO date string, or
    None if this user has no dated records at all."""
    latest = None

    def consider(value):
        nonlocal latest
        if not value:
            return
        value = str(value)[:10]
        if latest is None or value > latest:
            latest = value

    for table in word_tables:
        row = conn.execute(
            f'SELECT MAX(last_practiced), MAX(last_tartarus_completed), '
            f'MAX(leitner_last_reviewed) FROM "{table}"'
        ).fetchone()
        for value in row:
            consider(value)
    if table_exists(conn, 'mastery_events'):
        consider(conn.execute(
            'SELECT MAX(mastered_date) FROM mastery_events WHERE user=?', (user_s,),
        ).fetchone()[0])
    if table_exists(conn, sessions_table):
        consider(conn.execute(f'SELECT MAX(session_date) FROM "{sessions_table}"').fetchone()[0])
    if table_exists(conn, 'pending_drills'):
        consider(conn.execute(
            'SELECT MAX(created_at) FROM pending_drills WHERE user=?', (user_s,),
        ).fetchone()[0])
    return latest


def _user_has_unfinished_learning(conn, user_s, word_tables, today_iso):
    """True if this user has any outstanding learning obligation on any of
    their lists: material still short of mastery (Encoding), a due
    Consolidation Track step, or a due Spaced Maintenance review.

    This deliberately reuses consolidation_state_breakdown -- the one place
    that already knows what "due" means -- instead of re-deriving due-ness
    with its own SQL. Duplicating that logic here would be a second source
    of truth that silently drifts the first time the scheduler changes.
    """
    prefix = f'words_{user_s}_'
    for table in word_tables:
        if not table.startswith(prefix):
            continue
        lang = table[len(prefix):]
        try:
            state = consolidation_state_breakdown(user_s, lang, today=today_iso, conn=conn)
        except Exception:
            # A list whose material file is missing or unreadable can't be
            # assessed. Treat it as "nothing outstanding" rather than
            # letting an unrelated read failure decide to mutate dates:
            # this signal may only ever *widen* the shift condition, so
            # failing closed here is the conservative direction.
            continue
        if state['encoding'] or state['due_reinforcement'] or state['due_maintenance']:
            return True
    return False


def _shift_decision(conn, user_s, word_tables, sessions_table, today_date):
    """Decide whether to shift this user's dates, and by how much.

    Returns (should_shift, last_practiced, gap_days, shift_days, reason).

    A gap exists -- and is closed in a single call, landing every date on
    today -- when either of these holds:

    1. ``missed_day``: at least one whole calendar day passed with no
       practice at all (gap_days >= 2, i.e. the most recent practice is
       older than yesterday).
    2. ``unfinished_learning``: the learner practiced as recently as
       yesterday but still has outstanding work -- Encoding material below
       mastery, a due Consolidation Track step, or a due Leitner review.
       Without this, a learner who practises daily but never clears the
       board accumulates permanent overdue debt that the missed-day rule
       alone would never relieve.

    Practising *today* is always a no-op regardless of outstanding work:
    the shift distance would be zero, and there is nothing a shift could
    usefully do for a learner whose records are already current.
    """
    last_practiced = _user_last_practiced(conn, user_s, word_tables, sessions_table)
    if last_practiced is None:
        return False, None, None, 0, 'never_practiced'

    gap_days = (today_date - date.fromisoformat(last_practiced)).days
    if gap_days <= 0:
        return False, last_practiced, gap_days, 0, 'current'

    if gap_days >= 2:
        reason = 'missed_day'
    elif _user_has_unfinished_learning(conn, user_s, word_tables, today_date.isoformat()):
        reason = 'unfinished_learning'
    else:
        # Practised yesterday with nothing outstanding: not a gap. Shifting
        # here would hand out free time to a learner who is fully caught up.
        return False, last_practiced, gap_days, 0, 'current'

    latest = _user_max_shifted_date(conn, user_s, word_tables, sessions_table)
    shift_days = (today_date - date.fromisoformat(latest)).days if latest else 0
    if shift_days <= 0:
        # Something in the record is already dated today or later, so there
        # is no room to move without overshooting. Refuse rather than risk
        # a future-dated record.
        return False, last_practiced, gap_days, 0, 'no_room'
    return True, last_practiced, gap_days, shift_days, reason


def shift_user_dates_forward(user, *, today=None, database_file=None):
    """Idempotently bring one user's practice records up to today.

    Detects whether this user has a gap, and if so closes it completely in
    a single call: every practice-record date for this user moves forward
    by the same number of days, landing the most recent one exactly on
    ``today``. See _shift_decision for the two situations that count as a
    gap -- a whole calendar day missed, or work left unfinished since
    yesterday -- and for why practising today is always a no-op.

    Closing the whole gap at once (rather than one day per call) is what
    lets a single click restore a streak that a multi-day absence broke,
    which is the point of the feature. It stays idempotent because the
    shift is defined by the distance to today, not by a fixed step: once
    the records are current the next call finds no gap and does nothing.

    The shift distance is measured from the latest date held in ANY column
    being shifted, not from last_practiced. Since every shifted value is by
    definition <= that maximum, adding (today - maximum) to all of them
    cannot leave any single one dated later than today. That makes "never
    produce a future-dated record" a property of the arithmetic itself
    rather than something the caller has to get right.

    Due-ness everywhere in this app is date arithmetic (consolidation_step
    is step-based, but last_tartarus_completed/leitner_last_reviewed/
    mastered_date/session_date are all calendar dates); shifting every one of
    them together by the same amount moves the learner's whole history
    forward as a block, so the day that's covered reads exactly like a day
    that really was practiced through, rather than a gap.

    Touches, for this user only: each word list's last_practiced,
    last_tartarus_completed, and leitner_last_reviewed; mastery_events'
    mastered_date; the session log's session_date; and any pending drill's
    created_at. The user account's own created_at (when the account was
    made, not a practice date) is left untouched.

    Returns {'shifted': bool, 'last_practiced': str or None,
    'gap_days': int or None, 'shift_days': int, 'reason': str,
    'tables': {table_or_kind: rows_updated}}. ``gap_days`` and
    ``last_practiced`` describe the state *before* this call's decision,
    whether or not it ended up shifting anything. ``shift_days`` is how far
    everything actually moved (0 when nothing did) and ``reason`` is one of
    'missed_day', 'unfinished_learning', 'current', 'never_practiced', or
    'no_room'.

    Three extra safety measures beyond the gap check itself:

    - Every date column is validated against SQLite's own date() parser
      before anything is touched. date(bad_value, offset) silently returns
      NULL rather than erroring, which would quietly wipe out a value that
      wasn't a clean ISO date -- this turns that into a raised error
      instead, so nothing is ever silently lost.
    - The whole decision is recomputed a second time immediately after
      acquiring the write lock, right before applying anything. Two
      overlapping calls for the same user (a double-click, two browser
      tabs, two requests racing) serialize on that lock; whichever commits
      first lands the records on today, and the second -- now seeing the
      post-shift state -- finds no gap and does nothing. Because the
      second call recomputes its own shift distance from the state it
      actually observes, two racing shifts can never sum to more than the
      distance to today.
    - After applying the UPDATEs but before committing, every touched
      table's actual resulting dates are checked against ``today`` one
      more time: if anything would end up dated beyond today, the whole
      transaction is refused and rolled back. This is independent of and
      does not trust the gap-check logic above -- it's the last line of
      defense against this ever producing a future-dated record, which
      would silently stop being due forever, regardless of what caused
      it (a bug in the gap check, a clock anomaly, anything).

    Known limitation, shared with every other prefix-based lookup already
    in this file (export_user_data, the migration table scan, etc.): word
    tables are named words_<user>_<lang>, and a username that is itself a
    literal prefix of another username followed by "_" (e.g. "alice" and
    "alice_ann") is not distinguishable from a table name by prefix match
    alone. This only touches tables whose name is a true prefix match for
    this user; it does not (and structurally cannot, without a separate
    user/lang registry) further disambiguate that specific case.
    """
    user_s = sanitize_name(user, 'user')
    today_date = date.fromisoformat(today) if today else date.today()
    db_path = database_file or DATABASE_FILE
    conn = sqlite3.connect(db_path)
    try:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed before date shift.')
        if conn.execute('SELECT 1 FROM users WHERE name=?', (user_s,)).fetchone() is None:
            raise ValueError(f"Unknown user '{user_s}'.")

        # Escape literal underscores in the user name so LIKE's own
        # single-character wildcard can't blur one user's tables into
        # another's (e.g. "bahman" vs "bahmanx").
        word_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '\\' ORDER BY name",
            (f'words\\_{user_s}\\_%',),
        )]
        stable = sessions_table_name(user_s)
        should_shift, last_practiced, gap_days, shift_days, reason = _shift_decision(
            conn, user_s, word_tables, stable, today_date,
        )
        if not should_shift:
            return {
                'shifted': False, 'last_practiced': last_practiced, 'gap_days': gap_days,
                'shift_days': 0, 'reason': reason, 'tables': {},
            }

        word_date_columns = ['last_practiced', 'last_tartarus_completed', 'leitner_last_reviewed']
        unparseable = {}
        for table in word_tables:
            bad = _unparseable_date_columns(conn, table, word_date_columns)
            if bad:
                unparseable[table] = bad
        if table_exists(conn, 'mastery_events'):
            bad = conn.execute(
                'SELECT COUNT(*) FROM mastery_events WHERE user=? AND date(mastered_date) IS NULL',
                (user_s,),
            ).fetchone()[0]
            if bad:
                unparseable['mastery_events'] = ['mastered_date']
        if table_exists(conn, stable):
            bad = _unparseable_date_columns(conn, stable, ['session_date'])
            if bad:
                unparseable[stable] = bad
        if table_exists(conn, 'pending_drills'):
            bad = conn.execute(
                'SELECT COUNT(*) FROM pending_drills WHERE user=? AND date(created_at) IS NULL',
                (user_s,),
            ).fetchone()[0]
            if bad:
                unparseable['pending_drills'] = ['created_at']
        if unparseable:
            raise ValueError(
                f"Refusing to shift dates for '{user_s}': found values SQLite's date() "
                f"function can't parse (would be silently wiped instead of shifted): {unparseable}"
            )

        backup_path = verified_database_backup(db_path, 'date-shift')
        touched = {}

        conn.execute('BEGIN IMMEDIATE')

        # Re-decide under the lock, from scratch. A concurrent shift that
        # committed between the decision above and acquiring this lock
        # could have already closed the gap; only act on what is still
        # true right now. Recomputing the whole decision (not just the
        # gap) also recomputes shift_days, so two racing calls can never
        # add up to more than the distance to today.
        should_shift, last_practiced, gap_days, shift_days, reason = _shift_decision(
            conn, user_s, word_tables, stable, today_date,
        )
        if not should_shift:
            conn.rollback()
            return {
                'shifted': False, 'last_practiced': last_practiced, 'gap_days': gap_days,
                'shift_days': 0, 'reason': reason, 'tables': {},
            }
        offset = f'+{shift_days} days'

        for table in word_tables:
            cur = conn.execute(
                f'UPDATE "{table}" SET '
                'last_practiced = date(last_practiced, ?), '
                'last_tartarus_completed = date(last_tartarus_completed, ?), '
                'leitner_last_reviewed = date(leitner_last_reviewed, ?)',
                (offset, offset, offset),
            )
            touched[table] = cur.rowcount

        if table_exists(conn, 'mastery_events'):
            cur = conn.execute(
                'UPDATE mastery_events SET mastered_date = date(mastered_date, ?) WHERE user=?',
                (offset, user_s),
            )
            touched['mastery_events'] = cur.rowcount

        if table_exists(conn, stable):
            cur = conn.execute(f'UPDATE "{stable}" SET session_date = date(session_date, ?)', (offset,))
            touched[stable] = cur.rowcount

        if table_exists(conn, 'pending_drills'):
            cur = conn.execute(
                'UPDATE pending_drills SET created_at = date(created_at, ?) WHERE user=?',
                (offset, user_s),
            )
            touched['pending_drills'] = cur.rowcount

        # Absolute backstop, independent of the gap check above: whatever
        # happened, this must never be able to produce a date beyond today.
        # If that's ever violated -- a bug in the gap logic, a clock
        # anomaly, anything not already anticipated above -- refuse to
        # commit rather than risk it. A date pushed into the future isn't
        # a minor inconvenience like a missed step (which just waits);
        # nothing dated in the future would ever become due, which is a
        # much harder state to notice or recover from. ISO date strings
        # compare correctly as plain strings, so no parsing needed here.
        today_iso = today_date.isoformat()
        future_violations = {}
        for table in word_tables:
            row = conn.execute(
                f'SELECT MAX(last_practiced), MAX(last_tartarus_completed), MAX(leitner_last_reviewed) FROM "{table}"'
            ).fetchone()
            bad_cols = [col for col, value in zip(word_date_columns, row) if value and value > today_iso]
            if bad_cols:
                future_violations[table] = bad_cols
        if table_exists(conn, 'mastery_events'):
            value = conn.execute('SELECT MAX(mastered_date) FROM mastery_events WHERE user=?', (user_s,)).fetchone()[0]
            if value and value > today_iso:
                future_violations['mastery_events'] = ['mastered_date']
        if table_exists(conn, stable):
            value = conn.execute(f'SELECT MAX(session_date) FROM "{stable}"').fetchone()[0]
            if value and value > today_iso:
                future_violations[stable] = ['session_date']
        if table_exists(conn, 'pending_drills'):
            value = conn.execute('SELECT MAX(created_at) FROM pending_drills WHERE user=?', (user_s,)).fetchone()[0]
            if value and value > today_iso:
                future_violations['pending_drills'] = ['created_at']
        if future_violations:
            raise ValueError(
                f"Refusing to commit a date shift for '{user_s}' that would push a date beyond "
                f"today ({today_iso}): {future_violations}"
            )

        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Database integrity check failed after date shift.')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    log_event(
        'USER_DATES_SHIFTED', user=user_s, gap_days=gap_days, shift_days=shift_days,
        reason=reason, backup=backup_path,
        tables_touched=len(touched), rows_touched=sum(touched.values()),
    )
    return {
        'shifted': True, 'last_practiced': last_practiced, 'gap_days': gap_days,
        'shift_days': shift_days, 'reason': reason, 'tables': touched,
    }


_PRACTICE_ITEM_CACHE = {}
_PRACTICE_ITEM_CACHE_LOCK = threading.RLock()


def load_practice_items(path):
    """Load validated material, invalidating the process cache on file change."""
    path = os.path.abspath(os.fspath(path))
    stat = os.stat(path)
    signature = (stat.st_mtime_ns, stat.st_size)
    with _PRACTICE_ITEM_CACHE_LOCK:
        cached = _PRACTICE_ITEM_CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]
    raw_data = read_word_list(path)
    records = validate_word_list_items(raw_data['items'], path)
    items = []
    for position, record in enumerate(records):
        word = record['word']
        definition = normalize_definition(record.get('definition', record.get('translation', word)))
        frequency = normalize_word_frequency(record.get('word_frequency', 0))
        items.append({
            'content_id': record['id'],
            'word': word,
            'definition': definition,
            'word_frequency': frequency,
            'position': position,
            'kind': record.get('kind', 'item'),
            'record': record,
        })
    with _PRACTICE_ITEM_CACHE_LOCK:
        _PRACTICE_ITEM_CACHE[path] = (signature, items)
    return items


# --- Practice / Scoring Logic ---
# The lower an item's score, the more of its answer remains visible.
MAX_QUESTIONS = 16   # unique words per session (each asked exactly once)

LEITNER_INTERVALS = {box: box for box in range(1, 11)}  # box -> days until review

SCORE_DELTA = 0.5

def score_band(score):
    """Return the integer score band for a 0.0-9.0, half-point scale."""
    return min(9, max(0, int(float(score))))


def score_color_band(score):
    """Return the 3-way visual color band a score falls into: 1=red (<4),
    2=yellow (4-7.9), 3=green (>=8). This is a coarser grouping than
    score_band's 0-9 mastery band and is the one both score_gauge and any
    web gauge-color rendering must derive from, so there is exactly one
    definition of "what color is this score" in the codebase."""
    if score >= 8:
        return 3
    if score >= 4:
        return 2
    return 1


def score_gauge(score, ansi=True):
    """Returns a 3-dot growth gauge for a word's score.
    If ansi=True (default), includes ANSI color codes for terminal.
    If ansi=False, returns plain Unicode dots for web."""
    color = {1: Colors.RED, 2: Colors.YELLOW, 3: Colors.GREEN}[score_color_band(score)]
    if score >= 9:
        dots = '●●●'
    elif score >= 8:
        dots = '●●○'
    elif score >= 4:
        dots = '●○○'
    else:
        dots = '○○○'
    return dots if not ansi else f"{color}{dots}{Colors.ENDC}"


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

EFFORTFUL_RETRIEVAL_DRILL_TARGET = 2


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


def build_question_data(word_id, word_text, definition, score):
    """Build the ordinary question payload. Stage-specific presentation is added by Web."""
    band=score_band(score)
    question_type='learning' if band < 8 else 'production'
    full_lines=definition.split('\n') if definition else []
    primary=english_definition_only(definition)
    prompt=[primary] if primary else []
    # The example-sentence line always embeds the literal target word (see
    # DATASET_SCHEMA_GUIDE.md's two-line definition convention), so once
    # mask_sentence() starts hiding that word's own letters (score > 0), the
    # example line would just spell out what the mask is hiding. At score 0
    # the word itself is still shown in full, so there's nothing to protect.
    lines=full_lines if (question_type=='learning' and score<=0) else prompt
    return {
        'word_id':word_id,'word':mask_sentence(word_text,score),'word_unmasked':word_text,
        'definition':lines,'score':round(score,1),'gauge':score_gauge(score,ansi=False),
        'band':band,'gender':get_gender_style(word_text)[1],'type':question_type,
    }



def _load_progress_row(conn, table, word_id):
    row=conn.execute(
        f'SELECT score,leitner_box,last_tartarus_completed,leitner_last_reviewed FROM "{table}" WHERE id=?',
        (word_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f'Unknown practice item id: {word_id}')
    return row


def record_consolidation_answer(user, lang, word_id, correct, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,last_completed,leitner_last=_load_progress_row(conn,table,word_id); score=float(score or 0)
        if correct:
            new_score=min(9.0,score+SCORE_DELTA) if score<9 else 9.0
            new_box=box
            new_leitner_last=leitner_last
            # Mastery starts both review tracks together: Box 1 here, and
            # Consolidation Track reinforcement day 1 the next calendar day. The two
            # stay fully independent from this point on -- confirmed
            # deliberate (P3): completing one never satisfies or moves the
            # other, so the same word can be legitimately due for both a
            # reinforcement check-in and a Leitner review on the same date.
            if score < 9 <= new_score and box is None:
                new_box=1; new_leitner_last=today
            completed = today if new_score >= 9.0 else None
            # A reinforcement check-in (already mastered before this answer)
            # completes one Consolidation Track day; the encoding->mastery transition
            # itself does not -- day 1 begins on a later calendar date. Guard
            # against a duplicate/retried request double-advancing the same
            # calendar date's step.
            consolidation_step_delta = 1 if score >= 9.0 and str(last_completed or '')[:10] != today else 0
            conn.execute(
                f'UPDATE "{table}" SET score=?,leitner_box=?,leitner_last_reviewed=?,last_practiced=?,last_tartarus_completed=COALESCE(?,last_tartarus_completed), '
                'consolidation_step=MIN(10,consolidation_step+?), '
                'times_practiced=times_practiced+1,times_correct=times_correct+1 WHERE id=?',
                (new_score,new_box,new_leitner_last,today,completed,consolidation_step_delta,word_id),
            )
            if score < 9.0 <= new_score:
                record_mastery_event(conn, user, lang, word_id, 'mastered', today)
        else:
            new_score=score
            conn.execute(
                f'UPDATE "{table}" SET last_practiced=?,times_practiced=times_practiced+1,times_incorrect=times_incorrect+1 WHERE id=?',
                (today,word_id),
            )
        conn.commit()
    finally: conn.close()
    if correct and score < 9.0 <= new_score:
        log_event('WORD_MASTERED', user=user, lang=lang, word_id=word_id)
    log_event('CONSOLIDATION_ANSWER', user=user, lang=lang, word_id=word_id, correct=correct, score=new_score)
    return new_score


def complete_consolidation_drill(user, lang, word_id, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,last_completed,leitner_last=_load_progress_row(conn,table,word_id); score=float(score or 0)
        new_score=min(9.0,score+SCORE_DELTA) if score<9 else 9.0
        new_box=box; new_leitner_last=leitner_last
        if score < 9 <= new_score and box is None:
            new_box=1; new_leitner_last=today
        completed = today if new_score >= 9.0 else None
        consolidation_step_delta = 1 if score >= 9.0 and str(last_completed or '')[:10] != today else 0
        conn.execute(
            f'UPDATE "{table}" SET score=?,leitner_box=?,leitner_last_reviewed=?,last_practiced=?,last_tartarus_completed=COALESCE(?,last_tartarus_completed), '
            'consolidation_step=MIN(10,consolidation_step+?), '
            'times_practiced=times_practiced+1,times_drilled=times_drilled+1 WHERE id=?',
            (new_score,new_box,new_leitner_last,today,completed,consolidation_step_delta,word_id),
        )
        if score < 9.0 <= new_score:
            record_mastery_event(conn, user, lang, word_id, 'mastered', today)
        conn.commit()
    finally: conn.close()
    if score < 9.0 <= new_score:
        log_event('WORD_MASTERED', user=user, lang=lang, word_id=word_id)
    log_event('TARTARUS_DRILL_COMPLETED', user=user, lang=lang, word_id=word_id, score=new_score)
    return new_score


def record_maintenance_answer(user, lang, word_id, correct, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,_,_= _load_progress_row(conn,table,word_id)
        if float(score or 0) < 9:
            raise ValueError('Only score-9 items may enter Spaced Maintenance.')
        if correct:
            new_box=min(int(box or 1)+1,10)
            conn.execute(
                f'UPDATE "{table}" SET leitner_box=?,leitner_last_reviewed=?,last_practiced=?, '
                'times_practiced=times_practiced+1,times_correct=times_correct+1 WHERE id=?',
                (new_box,today,today,word_id),
            )
            if int(box or 1) < 10 <= new_box:
                record_mastery_event(conn, user, lang, word_id, 'box10', today)
        else:
            new_box=int(box or 1)
            conn.execute(
                f'UPDATE "{table}" SET last_practiced=?,times_practiced=times_practiced+1,times_incorrect=times_incorrect+1 WHERE id=?',
                (today,word_id),
            )
        conn.commit()
    finally: conn.close()
    log_event('LEITNER_ANSWER', user=user, lang=lang, word_id=word_id, correct=correct, box=new_box)
    return 9.0


def complete_maintenance_drill(user, lang, word_id, today=None):
    today=today or date.today().isoformat(); table=words_table_name(user,lang); conn=get_connection()
    try:
        score,box,_,_=_load_progress_row(conn,table,word_id)
        if float(score or 0) < 9: raise ValueError('Maintenance drill requires a score-9 item.')
        new_box=min(int(box or 1)+1,10)
        conn.execute(
            f'UPDATE "{table}" SET leitner_box=?,leitner_last_reviewed=?,last_practiced=?,times_practiced=times_practiced+1,times_drilled=times_drilled+1 WHERE id=?',
            (new_box,today,today,word_id),
        )
        if int(box or 1) < 10 <= new_box:
            record_mastery_event(conn, user, lang, word_id, 'box10', today)
        conn.commit()
    finally: conn.close()
    log_event('LEITNER_DRILL_COMPLETED', user=user, lang=lang, word_id=word_id, box=new_box)
    return 9.0




# --- Reporting ---
def log_session(user, lang, duration, practiced, correct, incorrect, drilled, mode=None, stage=None):
    conn = get_connection()
    table = ensure_sessions_table(conn, user)
    conn.execute(
        f'INSERT INTO "{table}" (language, session_date, duration_seconds, words_practiced, '
        f'correct_count, incorrect_count, drilled_count, mode, stage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (lang, date.today().isoformat(), duration, practiced, correct, incorrect, drilled, mode, stage)
    )
    conn.commit()
    conn.close()
    log_event('SESSION_LOGGED', user=user, lang=lang, duration=duration, practiced=practiced,
              correct=correct, incorrect=incorrect, drilled=drilled, mode=mode, stage=stage)


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




BACKUP_FORMAT = 'tartarus-progress'
BACKUP_VERSION = 4


def _table_rows(conn, table):
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return [dict(zip(columns, row)) for row in rows]


def export_user_data(user):
    """Read-only v4 logical backup export."""
    user_s=sanitize_name(user,'user'); conn=get_connection()
    try:
        row=conn.execute('SELECT name,created_at FROM users WHERE name=?',(user_s,)).fetchone()
        if row is None: raise ValueError(f"Unknown user '{user_s}'.")
        prefix=f'words_{user_s}_'; word_progress={}
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",(prefix+'%',)):
            word_progress[table[len(prefix):]]=_table_rows(conn,table)
        stable=sessions_table_name(user_s)
        sessions=_table_rows(conn,stable) if table_exists(conn,stable) else []
        mastery_events=[]
        if table_exists(conn,'mastery_events'):
            mastery_events=[dict(zip(MASTERY_EVENT_BACKUP_COLUMNS,r)) for r in conn.execute(
                'SELECT lang,word_id,event_type,mastered_date FROM mastery_events '
                'WHERE user=? ORDER BY mastered_date,id',
                (user_s,),
            )]
        result={'format':BACKUP_FORMAT,'version':BACKUP_VERSION,'user':{'name':row[0],'created_at':row[1]},'word_progress':word_progress,'sessions':sessions,'mastery_events':mastery_events}
    finally: conn.close()
    log_event('USER_DATA_EXPORTED', user=user_s, lists=len(word_progress), sessions=len(sessions))
    return result



def _validate_backup_rows(rows, allowed_columns, label):
    if not isinstance(rows, list):
        raise ValueError(f'{label} must be an array.')
    validated = []
    required = set(allowed_columns)
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f'{label} row {number} must be an object.')
        keys = set(row)
        unknown = keys - required
        missing = required - keys
        if unknown or missing:
            detail = []
            if unknown:
                detail.append(f"unknown: {', '.join(sorted(unknown))}")
            if missing:
                detail.append(f"missing: {', '.join(sorted(missing))}")
            raise ValueError(f"{label} row {number} has invalid columns ({'; '.join(detail)}).")
        validated.append({column: row[column] for column in allowed_columns})
    return validated


def import_user_data(user, data):
    """Atomically import logical backup versions 1 through 4."""
    user_s=sanitize_name(user,'user')
    version=data.get('version') if isinstance(data,dict) else None
    if not isinstance(data,dict) or data.get('format')!=BACKUP_FORMAT or version not in (1,2,3,4):
        raise ValueError('Unsupported backup format or version.')
    backup_user=data.get('user')
    if not isinstance(backup_user,dict) or backup_user.get('name')!=user_s: raise ValueError('Backup user does not match the requested user.')
    wp=data.get('word_progress'); sessions=data.get('sessions')
    events=data.get('mastery_events',[]) if version<3 else data.get('mastery_events')
    if not isinstance(wp,dict) or not isinstance(sessions,list) or not isinstance(events,list):
        raise ValueError('Backup must include word_progress, sessions, and mastery_events arrays.')
    initialize_database(create_backup=False)
    conn=get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE'); ensure_user(conn,user_s); st=ensure_sessions_table(conn,user_s); ensure_mastery_events_table(conn)
        session_cols=_word_table_columns(conn,st); session_rows=_validate_backup_rows(sessions,session_cols,'sessions')
        prepared={}
        event_rows=_validate_backup_rows(events,MASTERY_EVENT_BACKUP_COLUMNS,'mastery_events')
        for lang,rows in wp.items():
            lang_s=sanitize_name(str(lang),'language'); table=ensure_word_table(conn,user_s,lang_s)
            converted=[]
            for raw in rows:
                if not isinstance(raw,dict): raise ValueError(f'word_progress.{lang_s} rows must be objects.')
                if data.get('version')==1:
                    score=float(raw.get('score',0) or 0); box=raw.get('leitner_box'); last=raw.get('last_practiced')
                    converted.append({
                        'id':raw.get('id'),'content_id':raw.get('content_id'),'score':score,'last_practiced':last,
                        'last_tartarus_completed':last,'active':raw.get('active',1),'times_practiced':raw.get('times_practiced',0),
                        'times_correct':raw.get('times_correct',0),'times_incorrect':raw.get('times_incorrect',0),'times_drilled':raw.get('times_drilled',0),
                        'times_mastered':raw.get('times_mastered',0),'leitner_box':box,
                        'leitner_last_reviewed':last if score>=9 and box is not None else None,
                        # 'gauntlet_completed_day' is the pre-rename field name --
                        # accept backups exported before this rename too.
                        'consolidation_step':raw.get('consolidation_step',raw.get('gauntlet_completed_day',0)),
                    })
                else:
                    # Every backup version older than this field's introduction
                    # is missing it; default to 0 rather than reject the
                    # import outright -- the word simply restarts its 10-day
                    # reinforcement count, which is conservative (never
                    # overstates progress) and not a data loss, since score
                    # and Leitner state are unaffected. Backups exported
                    # before the Consolidation Track rename used the field
                    # name 'gauntlet_completed_day' for the same value.
                    row=dict(raw)
                    if 'consolidation_step' not in row and 'gauntlet_completed_day' in row:
                        row['consolidation_step']=row.pop('gauntlet_completed_day')
                    row.setdefault('consolidation_step',0)
                    converted.append(row)
            prepared[table]=_validate_backup_rows(converted,WORD_TABLE_COLUMNS,f'word_progress.{lang_s}')
        prefix=f'words_{user_s}_'
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",(prefix+'%',)).fetchall():
            if table not in prepared: conn.execute(f'DROP TABLE "{table}"')
        for table,rows in prepared.items():
            conn.execute(f'DELETE FROM "{table}"')
            if rows:
                q=', '.join(f'"{c}"' for c in WORD_TABLE_COLUMNS); ph=', '.join('?' for _ in WORD_TABLE_COLUMNS)
                conn.executemany(f'INSERT INTO "{table}" ({q}) VALUES ({ph})',[[r[c] for c in WORD_TABLE_COLUMNS] for r in rows])
        conn.execute(f'DELETE FROM "{st}"')
        if session_rows:
            q=', '.join(f'"{c}"' for c in session_cols); ph=', '.join('?' for _ in session_cols)
            conn.executemany(f'INSERT INTO "{st}" ({q}) VALUES ({ph})',[[r[c] for c in session_cols] for r in session_rows])
        conn.execute('DELETE FROM mastery_events WHERE user=?',(user_s,))
        if event_rows:
            conn.executemany(
                'INSERT INTO mastery_events(user,lang,word_id,event_type,mastered_date) VALUES(?,?,?,?,?)',
                [(user_s,r['lang'],r['word_id'],r['event_type'],r['mastered_date']) for r in event_rows],
            )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    log_event('USER_DATA_IMPORTED', user=user_s, lists=len(prepared), sessions=len(session_rows))



def save_custom_list(user, list_name, items):
    """Validate and atomically save a personal imported material list."""
    user_s = sanitize_name(user, 'user')
    list_name_s = sanitize_name(list_name, 'list')
    if isinstance(items, dict):
        data = items
    elif isinstance(items, list):
        data = {
            'metadata': canonical_material_metadata(name=list_name_s),
            'items': items,
        }
    else:
        raise ValueError('Custom list must be a JSON object or item array.')
    if not isinstance(data.get('metadata'), dict) or not isinstance(data.get('items'), list):
        raise ValueError('Custom list must contain metadata and an items array.')
    file_path = word_list_path_user_specific(user_s, list_name_s)
    # Personal imports follow the same Master Schema contract as bundled material:
    # source items may omit IDs.  Persist generated IDs into the personal copy on
    # first import so later edits keep the same progress identity.
    validated_items = validate_word_list_items(data['items'], file_path)
    data = {
        'metadata': canonical_material_metadata(data['metadata'], name=list_name_s),
        'items': validated_items,
    }
    write_word_list_atomic(file_path, data)
    sync_word_list(user_s, list_name_s)
    log_event('CUSTOM_LIST_SAVED', user=user_s, lang=list_name_s, items=len(validated_items))
    return list_name_s

