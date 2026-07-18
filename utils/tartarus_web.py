# -*- coding: utf-8 -*-
"""
Tartarus web server: a localhost-only JSON API + static frontend that wraps
the same SQLite-backed scoring logic as the tartarus.py CLI. Standard
library only - no extra packages needed.

Run via: make web   (serves http://127.0.0.1:9999)
"""
import os
import sys
import errno
import json
import time
import random
import urllib.parse
import http.server
import uuid
from pathlib import Path

from datetime import date, timedelta
import tartarus as ll

HOST = '127.0.0.1'
PORT = 9999

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJECT_DIR, 'web')

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}


# In-memory practice sessions, keyed by a random session id. Lost on
# restart, which is fine - sessions are short-lived and progress is only
# persisted to the database when a word is answered or the session ends.
SESSIONS = {}


MAX_QUESTIONS = ll.MAX_QUESTIONS
DRILL_WORDS = ll.DRILL_WORDS
DRILL_TARGET = 9


def drill_definition_lines(current):
    """Return the definition shown while a word is being drilled."""
    prompt = (
        current.get('drill_definition')
        or current.get('prompt_definition')
        or current.get('definition')
        or ''
    )
    return prompt.split('\n') if prompt else []


def gauge_dots(score):
    """Return the compact score gauge used by word-list API responses."""
    if score >= 9:
        return '●●●'
    if score >= 7:
        return '●●○'
    if score >= 4:
        return '●○○'
    return '○○○'


# --- Session lifecycle ---
def mastered_words(user, lang):
    """Read all mastered entries, ordered by their last Fast review."""
    conn = ll.get_connection()
    table = ll.ensure_fast_review_column(conn, user, lang)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            return []
        conn.commit()
        return conn.execute(
            f'''SELECT id, text, definition, score, leitner_box
                FROM "{table}"
                WHERE active = 1 AND score >= 9
                ORDER BY CASE WHEN last_fast_review_at IS NULL THEN 0 ELSE 1 END,
                         last_fast_review_at,
                         COALESCE(last_practiced, '0000-00-00'), id'''
        ).fetchall()
    finally:
        conn.close()


def start_session(user, lang, audio_lang=None, drill_all=False, drill_mode=False, known_drill_mode=False, instant_drill=False, fast_mode=False, wpm=128):
    sentence_mode = ll.is_sentence_list(lang)
    selected_drill_modes = sum(bool(value) for value in (drill_all, drill_mode, known_drill_mode, instant_drill))
    if selected_drill_modes > 1:
        raise ValueError("Choose only one drill mode per session.")
    if fast_mode:
        if selected_drill_modes:
            raise ValueError("Fast mode cannot be combined with drill modes.")
        words = mastered_words(user, lang)
        if not words:
            raise ValueError("No mastered words are available for fast mode.")
    else:
        ll.sync_word_list(user, lang)
        words = ll.get_words_for_practice(
            user, lang,
            DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS,
            drill_mode=drill_mode,
            known_drill_mode=known_drill_mode,
        )
    if sentence_mode and (drill_all or drill_mode or known_drill_mode or instant_drill):
        raise ValueError("Sentence lists do not support drill modes.")
    voice_lang = audio_lang or lang

    queue = [
        {'word_id': r[0], 'word_text': r[1], 'definition': r[2], 'score': r[3], 'leitner_box': r[4]}
        for r in words
    ]

    session_id = uuid.uuid4().hex
    session = {
        'user': user,
        'lang': lang,
        'voice_lang': voice_lang,
        'wpm': wpm,
        'queue': queue,
        'total': len(queue),
        'practiced': 0,
        'max_questions': len(queue) if fast_mode else (DRILL_WORDS if (drill_mode or drill_all) else MAX_QUESTIONS),
        'drill_mode': drill_mode,
        'known_drill_mode': known_drill_mode,
        'instant_drill': instant_drill,
        'fast_mode': fast_mode,
        'drill_all': drill_all,
        'sentence_mode': sentence_mode,
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'start_time': time.time(),
        'current': None,
    }
    SESSIONS[session_id] = session
    return session_id, session


def next_question(session):
    queue = session['queue']
    if not queue:
        return None
    entry = queue.pop(0)
    question, drill = ll.build_question_data(
        entry['word_id'], entry['word_text'],
        entry['definition'], entry['score'], entry['leitner_box'],
        sentence_mode=session.get('sentence_mode', False),
        fast_mode=session.get('fast_mode', False),
        drill_mode=(session.get('drill_mode', False) or session.get('drill_all', False)),
        known_drill_mode=session.get('known_drill_mode', False),
    )
    if session.get('known_drill_mode'):
        # The known-drill prompt must not leak the answer through the API.
        question['word'] = ''
        question['word_unmasked'] = ''
        if question.get('drill_start'):
            question['drill_start']['word'] = ''
    session['current'] = {
        'word_id': entry['word_id'],
        'word_text': entry['word_text'],
        'definition': entry['definition'],
        'prompt_definition': '\n'.join(question['definition']),
        'drill_definition': '\n'.join(question['definition']),
        'score': entry['score'],
        'leitner_box': entry['leitner_box'],
        'type': question['type'],
        'drill': drill,
    }
    return question


def finalize_session(session, ended_early=False):
    elapsed = int(time.time() - session['start_time'])
    if session['practiced'] > 0:
        ll.log_session(
            session['user'], session['lang'], elapsed, session['practiced'],
            session['correct'], len(session['incorrect']), session['drilled']
        )
    practiced = session['practiced']
    attempts = practiced + len(session['incorrect']) if session.get('fast_mode') else practiced
    return {
        'practiced': session['practiced'],
        'correct': session['correct'],
        'incorrect': session['incorrect'],
        'drilled': session['drilled'],
        'elapsed_seconds': elapsed,
        'ended_early': ended_early,
        'fast_mode': session.get('fast_mode', False),
        'accuracy': round(100 * session['correct'] / attempts, 1) if attempts else None,
        'avg_seconds_per_item': round(elapsed / practiced, 1) if practiced else None,
    }


def advance_fast(session, correct, attempt):
    cur = session['current']
    word_text = cur['word_text']
    if not correct:
        session['incorrect'].append({'word': word_text, 'attempt': attempt})
        return {
            'result': 'incorrect',
            'message': f"Incorrect. Try again. Mistakes: {len(session['incorrect'])}",
            'word': word_text,
            'fast_mode': True,
            'fast_retry': True,
            'done': False,
            'incorrect_count': len(session['incorrect']),
        }

    session['practiced'] += 1
    session['correct'] += 1
    status = 'correct'
    message = 'Correct.'

    result = {'result': status, 'message': message, 'word': word_text, 'fast_mode': True}
    if session['practiced'] >= session['max_questions']:
        result['done'] = True
        result['session'] = finalize_session(session)
    else:
        result['done'] = False
        result['question'] = next_question(session)
        result['progress'] = {
            'correct': session['correct'],
            'drilled': 0,
            'total': session['total'],
            'questions': session['practiced'],
            'max_questions': session['max_questions'],
        }
    return result


def advance(session, status, message, attempt=None):
    cur = session['current']
    word_text = cur['word_text']
    session['practiced'] += 1
    if status == 'correct':
        session['correct'] += 1
    elif status == 'incorrect':
        session['incorrect'].append({'word': word_text, 'attempt': attempt})
    elif status == 'drilled':
        session['drilled'] += 1

    result = {'result': status, 'message': message, 'word': word_text}
    limit_reached = session['practiced'] >= session['max_questions']
    nxt = None if limit_reached else next_question(session)
    if nxt is None:
        result['done'] = True
        result['session'] = finalize_session(session)
    else:
        result['done'] = False
        result['question'] = nxt
        result['progress'] = {
            'correct': session['correct'],
            'drilled': session['drilled'],
            'total': session['total'],
            'questions': session['practiced'],
            'max_questions': session['max_questions'],
        }
    return result


def process_drill_answer(session, answer):
    cur = session['current']
    drill = cur['drill']
    if answer == '!!':
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if ll.answer_matches(answer, cur['word_text']):
        drill['correct_in_a_row'] += 1
        if drill['correct_in_a_row'] >= DRILL_TARGET:
            cur['drill'] = None
            if session.get('drill_mode') or session.get('known_drill_mode') or drill.get('instant'):
                ll.record_as_drilled(
                    session['user'], session['lang'], cur['word_id'],
                    known_review=session.get('known_drill_mode', False)
                )
                return advance(session, 'drilled', "Drill complete.")
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'drilled')
            return advance(session, 'drilled', "Drill complete. Score set to 5.0.")
        correct = True
    else:
        drill['correct_in_a_row'] = 0
        correct = False

    drill['repetition'] += 1
    show_word = not session.get('known_drill_mode')
    return {
        'result': 'drill_progress',
        'done': False,
        'drill': {
            'word': cur['word_text'] if show_word else '',
            'definition': drill_definition_lines(cur),
            'repetition': drill['repetition'],
            'correct_in_a_row': drill['correct_in_a_row'],
            'target': DRILL_TARGET,
            'correct': correct,
            'show_word': not session.get('known_drill_mode'),
        },
    }


def process_answer(session, answer):
    answer = (answer or '').strip()
    cur = session['current']
    sentence_mode = session.get('sentence_mode', False)

    # Session-level commands are always honoured, even mid-drill.
    if answer == '!!':
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if session.get('fast_mode'):
        correct = ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)
        if correct:
            ll.record_fast_review(session['user'], session['lang'], cur['word_id'])
        return advance_fast(session, correct, answer)

    if answer.startswith('@'):
        if not (session.get('drill_mode') or session.get('known_drill_mode')):
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'mastered')
        elif session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], session['lang'], cur['word_id'])
        return advance(session, 'mastered', f"Marked '{cur['word_text']}' as known.")

    if answer.startswith('!'):
        if not (session.get('drill_mode') or session.get('known_drill_mode')):
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'flagged')
        elif session.get('known_drill_mode'):
            ll.record_known_review_seen(session['user'], session['lang'], cur['word_id'])
        return advance(session, 'flagged', f"Flagged '{cur['word_text']}' for more practice.")

    if cur['drill'] is not None:
        return process_drill_answer(session, answer)

    if answer.startswith('$'):
        # Drill is disabled for sentence practice (sentences are too long to drill).
        if sentence_mode:
            ll.update_sentence_score(session['user'], session['lang'], cur['word_id'],
                                     False, cur['score'], cur['leitner_box'])
            session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
            return {
                'result': 'sentence_retry',
                'done': False,
                'message': "Incorrect. Try one more time.",
                'word': cur['word_text'],
            }
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1}
        return {
            'result': 'drill_start',
            'done': False,
            'drill': {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'show_word': not session.get('known_drill_mode'),
            },
        }

    correct = ll.answer_matches(answer, cur['word_text'], sentence_mode=sentence_mode)

    if session.get('known_drill_mode'):
        ll.record_review_result(session['user'], session['lang'], cur['word_id'], correct)
        if correct:
            return advance(session, 'correct', None, attempt=answer)
        session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1}
        return {
            'result': 'drill_start',
            'done': False,
            'drill': {
                'word': '',
                'definition': drill_definition_lines(cur),
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'correct': False,
                'show_word': False,
            },
        }

    if sentence_mode:
        ll.update_sentence_score(session['user'], session['lang'], cur['word_id'],
                                 correct, cur['score'], cur['leitner_box'])
        if correct:
            return advance(session, 'correct', None, attempt=answer)
        session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
        return {
            'result': 'sentence_retry',
            'done': False,
            'message': "Incorrect. Try one more time.",
            'word': cur['word_text'],
        }

    if correct:
        ll.update_word_score(session['user'], session['lang'], cur['word_id'],
                             'correct', cur['score'], cur['leitner_box'])
        return advance(session, 'correct', None, attempt=answer)

    ll.update_word_score(session['user'], session['lang'], cur['word_id'],
                         'incorrect', cur['score'], cur['leitner_box'])
    if session.get('instant_drill'):
        session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1, 'instant': True}
        return {
            'result': 'drill_start',
            'done': False,
            'drill': {
                'word': cur['word_text'],
                'definition': drill_definition_lines(cur),
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
                'correct': False,
                'show_word': True,
            },
        }
    return advance(session, 'incorrect', f"Incorrect. The word was: {cur['word_text']}", attempt=answer)


# --- Word lists / report ---
def list_word_lists():
    """Return shared lists grouped by language/type for cascading selectors.

    Each entry keeps ``lang`` as the database/list identifier, while
    ``category`` is one of ``english_vocabulary``, ``english_sentences``,
    ``german_vocabulary``, or ``german_sentences``.
    """
    if not os.path.isdir(ll.WORD_LISTS_DIR):
        return []

    conn = ll.get_connection()
    user_tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'sessions_%'"
    ).fetchall()
    conn.close()
    known_users = {table[0].removeprefix('sessions_') for table in user_tables}
    result = []

    word_lists_dir = Path(ll.WORD_LISTS_DIR)
    for path in sorted(word_lists_dir.rglob('*.json')):
        relative = path.relative_to(word_lists_dir)
        parts = relative.parts
        if len(parts) >= 3 and parts[0] in ('english', 'german') and parts[1] in ('vocabulary', 'sentences'):
            language, kind, level = parts[0], parts[1], parts[2]
            if level not in ('a1', 'a2', 'b1', 'b2', 'c1', 'c2'):
                continue
            lang = path.stem
            category = f'{language}_{kind}'
            for user in known_users:
                result.append({
                    'user': user, 'lang': lang, 'language': language,
                    'kind': kind, 'level': level, 'category': category, 'shared': True,
                })
            continue

        # Keep user-created root files visible for editing and compatibility.
        if len(parts) == 1 and '_' in path.stem:
            first, rest = path.stem.split('_', 1)
            if first in known_users:
                result.append({
                    'user': first, 'lang': rest, 'language': rest.split('_', 1)[0],
                    'kind': 'sentences' if ll.is_sentence_list(rest) else 'vocabulary',
                    'category': f"{rest.split('_', 1)[0]}_{'sentences' if ll.is_sentence_list(rest) else 'vocabulary'}",
                    'shared': False,
                })

    unique = {}
    for item in result:
        key = (item['user'], item['lang'])
        if key not in unique or not item['shared']:
            unique[key] = item
    return [unique[key] for key in sorted(unique)]

def report_data(user, lang=None):
    data = ll.get_session_totals(user, lang)
    if not data:
        return []
    # get_session_totals returns data for a specific lang; we need to format it
    if lang:
        # Single language - just wrap the data
        return [{
            'language': lang,
            'days': data['days'],
            'total': data['total'],
        }]
    else:
        # All languages - need to query per language
        user_s = ll.sanitize_name(user, 'user')
        table = f"sessions_{user_s}"
        conn = ll.get_connection()
        cursor = conn.execute(f'SELECT DISTINCT language FROM "{table}" ORDER BY language')
        languages = [row[0] for row in cursor.fetchall()]
        conn.close()
        reports = []
        for language in languages:
            ld = ll.get_session_totals(user, language)
            if ld:
                reports.append({
                    'language': language,
                    'days': ld['days'],
                    'total': ld['total'],
                })
        return reports


def user_summary_data(user):
    return ll.get_user_summary_data(user)


def user_progress_data(user, category=None, level=None):
    return ll.get_user_progress_data(user, category, level)


def leitner_stats_data(user, lang):
    return ll.get_leitner_distribution(user, lang)


def dashboard_data(user, lang=None):
    """All analytics data for the dashboard: overview, velocity, and (if lang
    given) mastery funnel, nemesis words, and per-list completion prediction."""
    user_s = ll.sanitize_name(user, 'user')
    lang_s = ll.sanitize_name(lang, 'language') if lang else None
    sessions_table = f"sessions_{user_s}"
    conn = ll.get_connection()

    has_sessions = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (sessions_table,)
    ).fetchone() is not None

    total_seconds = total_practiced = total_correct = total_incorrect = 0
    current_streak = best_streak = 0
    avg_seconds_per_word = avg_words_7d = avg_seconds_7d = 0.0
    session_count = distinct_days = 0

    # Scope session queries to the selected list when lang is given
    s_where = f'WHERE language = ?' if lang_s else ''
    s_params = (lang_s,) if lang_s else ()
    s_and_lang = f'AND language = ?' if lang_s else ''

    if has_sessions:
        t = conn.execute(
            f'SELECT SUM(duration_seconds), SUM(words_practiced), '
            f'SUM(correct_count), SUM(incorrect_count) FROM "{sessions_table}" {s_where}',
            s_params
        ).fetchone()
        total_seconds = t[0] or 0
        total_practiced = t[1] or 0
        total_correct = t[2] or 0
        total_incorrect = t[3] or 0

        all_dates = [r[0] for r in conn.execute(
            f'SELECT session_date FROM "{sessions_table}" {s_where}', s_params).fetchall()]
        current_streak, best_streak = ll.compute_streak(all_dates)
        distinct_days = len(set(all_dates))
        session_count = len(all_dates)

        last_7 = conn.execute(
            f"SELECT SUM(words_practiced), SUM(duration_seconds) FROM \"{sessions_table}\" "
            f"WHERE session_date >= date('now', '-6 days', 'localtime') {s_and_lang}",
            (lang_s,) if lang_s else ()
        ).fetchone()
        avg_words_7d = (last_7[0] or 0) / 7.0
        avg_seconds_7d = (last_7[1] or 0) / 7.0
        if total_practiced > 0:
            avg_seconds_per_word = total_seconds / total_practiced

    total_answers = total_correct + total_incorrect
    overall_accuracy = round(100 * total_correct / total_answers, 1) if total_answers > 0 else None

    # --- Due today: scoped to selected list, or all lists if no lang ---
    if lang_s:
        tname = f"words_{user_s}_{lang_s}"
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tname,)
        ).fetchone():
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tname}")').fetchall()}
            if 'leitner_box' in cols:
                due_today_total = conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1 AND ("
                    f"last_practiced IS NULL OR "
                    f"julianday('now','localtime') - julianday(last_practiced) >= "
                    f"CASE leitner_box WHEN 1 THEN 1 WHEN 2 THEN 2 WHEN 3 THEN 4 "
                    f"WHEN 4 THEN 9 ELSE 14 END)"
                ).fetchone()[0]
            else:
                due_today_total = conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1"
                ).fetchone()[0]
        else:
            due_today_total = 0
    else:
        prefix = f"words_{user_s}_"
        word_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
            (f"{prefix}%",)
        ).fetchall()
        due_today_total = 0
        for (tname,) in word_tables:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tname}")').fetchall()}
            if 'leitner_box' in cols:
                due_today_total += conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1 AND ("
                    f"last_practiced IS NULL OR "
                    f"julianday('now','localtime') - julianday(last_practiced) >= "
                    f"CASE leitner_box WHEN 1 THEN 1 WHEN 2 THEN 2 WHEN 3 THEN 4 "
                    f"WHEN 4 THEN 9 ELSE 14 END)"
                ).fetchone()[0]
            else:
                due_today_total += conn.execute(
                    f"SELECT COUNT(*) FROM \"{tname}\" WHERE active=1"
                ).fetchone()[0]

    # Benchmark pace vs. 20 words/day standard
    if avg_words_7d >= 40:
        benchmark = 'Hyper-Learner'
    elif avg_words_7d >= 20:
        benchmark = 'On Track'
    elif avg_words_7d >= 10:
        benchmark = 'Building Momentum'
    elif avg_words_7d > 0:
        benchmark = 'Getting Started'
    else:
        benchmark = None

    result = {
        'overview': {
            'streak': {'current': current_streak, 'best': best_streak},
            'total_seconds': total_seconds,
            'overall_accuracy': overall_accuracy,
            'due_today': due_today_total,
        },
        'velocity': {
            'avg_seconds_per_word': round(avg_seconds_per_word, 1) if avg_seconds_per_word else None,
            'avg_words_per_day_7d': round(avg_words_7d, 1),
            'avg_minutes_per_day_7d': round(avg_seconds_7d / 60, 1),
            'benchmark': benchmark,
            'enough_data': session_count >= 3,
        },
        'mastery': None,
        'nemesis': None,
        'prediction': None,
    }

    # --- Per-list data (requires lang) ---
    if lang_s:
        wtable = f"words_{user_s}_{lang_s}"
        has_wtable = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (wtable,)
        ).fetchone() is not None

        if has_wtable:
            wcols = {r[1] for r in conn.execute(f'PRAGMA table_info("{wtable}")').fetchall()}
            has_leitner = 'leitner_box' in wcols

            # Mastery funnel: Learning (1–3.9), Familiar (4–8.9), Mastered (9.0)
            f_row = conn.execute(
                f'SELECT SUM(CASE WHEN score < 4.0 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN score >= 4.0 AND score < 9.0 THEN 1 ELSE 0 END), '
                f'SUM(CASE WHEN score >= 9.0 THEN 1 ELSE 0 END), COUNT(*) '
                f'FROM "{wtable}" WHERE active=1'
            ).fetchone()
            learning, familiar, mastered_count, total_words = f_row
            result['mastery'] = {
                'learning': learning or 0,
                'familiar': familiar or 0,
                'mastered': mastered_count or 0,
                'total': total_words or 0,
            }

            # Nemesis: top-10 hardest words by incorrect count
            result['nemesis'] = [
                {'word': r[0], 'times_incorrect': r[1], 'times_correct': r[2],
                 'score': round(r[3], 1)}
                for r in conn.execute(
                    f'SELECT text, times_incorrect, times_correct, score FROM "{wtable}" '
                    f'WHERE active=1 AND times_incorrect > 0 '
                    f'ORDER BY times_incorrect DESC, score ASC LIMIT 10'
                ).fetchall()
            ]

            # Prediction: grind hours + calendar date when all words reach box 5
            enough_data = session_count >= 3 and avg_seconds_per_word and avg_seconds_per_word > 0
            sentence_mode = ll.is_sentence_list(lang_s)
            if enough_data:
                box_col = 'leitner_box' if has_leitner else '1'
                word_rows = conn.execute(
                    f'SELECT score, {box_col} FROM "{wtable}" WHERE active=1'
                ).fetchall()

                # Total corrects needed → grind hours
                total_corrects = sum(ll.corrects_to_mastery(s, sentence_mode=sentence_mode) for s, _ in word_rows)
                grind_hours = round(total_corrects * avg_seconds_per_word / 3600, 1)

                # Calendar date: today + max(grind_days + leitner_days) over all words
                avg_secs_per_day = avg_seconds_7d if avg_seconds_7d > 0 else (
                    total_seconds / distinct_days if distinct_days > 0 else 3600
                )
                max_days = 0.0
                for score, box in word_rows:
                    b = int(box) if box else 1
                    corrects = ll.corrects_to_mastery(score, sentence_mode=sentence_mode)
                    # After reaching score 9, words advance through remaining Leitner boxes
                    leitner_days = sum(
                        ll.LEITNER_INTERVALS.get(bb, 14) for bb in range(b, 5)
                    )
                    total_days = grind_days + leitner_days
                    if total_days > max_days:
                        max_days = total_days

                box5_date = (date.today() + timedelta(days=int(max_days))).isoformat()
                result['prediction'] = {
                    'grind_hours': grind_hours,
                    'box5_date': box5_date,
                    'enough_data': True,
                }
            else:
                result['prediction'] = {
                    'grind_hours': None,
                    'box5_date': None,
                    'enough_data': False,
                    'sessions_needed': max(0, 3 - session_count),
                }

    conn.close()
    return result


def word_list_stats(user, lang, due_today_only=False):
    table = ll.words_table_name(user, lang)
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None
    ll.ensure_word_table(conn, user, lang)
    
    today = date.today()
    if due_today_only:
        # Only select words that are due today (next review is today or earlier)
        query = f'''
            SELECT text, score, active, times_practiced, times_correct, times_incorrect,
                   times_drilled, times_mastered, last_practiced, leitner_box, last_known_review_at
            FROM "{table}" WHERE active = 1 AND (
                last_practiced IS NULL OR
                julianday(?, 'localtime') - julianday(last_practiced) >=
                CASE leitner_box WHEN 1 THEN 1 WHEN 2 THEN 2 WHEN 3 THEN 4 WHEN 4 THEN 9 ELSE 14 END
            ) ORDER BY score ASC, text ASC
        '''
        rows = conn.execute(query, (today.isoformat(),)).fetchall()
    else:
        rows = conn.execute(
            f'SELECT text, score, active, times_practiced, times_correct, times_incorrect, '
            f'times_drilled, times_mastered, last_practiced, leitner_box, last_known_review_at '
            f'FROM "{table}" ORDER BY active DESC, score ASC, text ASC'
        ).fetchall()
    
    conn.close()
    words = []
    for (text, score, active, practiced, correct, incorrect,
         drilled, mastered, last_practiced, leitner_box, last_known_review_at) in rows:
        box = leitner_box or 1
        if last_practiced:
            interval = ll.LEITNER_INTERVALS.get(box, 1)
            next_review = (date.fromisoformat(last_practiced) + timedelta(days=interval)).isoformat()
        else:
            next_review = None
        words.append({
            'word': text,
            'score': round(score, 1),
            'gauge': gauge_dots(score),
            'band': ll.score_band(score),
            'active': bool(active),
            'leitner_box': box,
            'next_review': next_review,
            'times_practiced': practiced,
            'times_correct': correct,
            'times_incorrect': incorrect,
            'times_drilled': drilled,
            'times_mastered': mastered,
            'last_practiced': last_practiced,
            'last_known_review_at': last_known_review_at,
        })
    return words


def load_word_list(user, lang):
    path = ll.word_list_path(user, lang)
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    words = []
    for entry in data:
        definition = entry.get('definition') or []
        if isinstance(definition, str):
            definition = [definition] if definition else []
        words.append({
            'word': entry.get('word', ''),
            'def1': definition[0] if len(definition) > 0 else '',
            'def2': definition[1] if len(definition) > 1 else '',
        })
    return words


def save_word_list(user, lang, items):
    # Always save to user-specific file
    path = ll.word_list_path_user_specific(user, lang)
    data = []
    for item in items:
        word = str(item.get('word', '')).strip()
        if not word:
            continue
        defs = [str(item.get(f, '')).strip() for f in ('def1', 'def2')]
        defs = [d for d in defs if d]
        entry = {'word': word}
        if len(defs) == 1:
            entry['definition'] = defs[0]
        elif len(defs) > 1:
            entry['definition'] = defs
        data.append(entry)
    os.makedirs(ll.WORD_LISTS_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    ll.sync_word_list(user, lang)
    return path, len(data)


def init_word_list(user, lang):
    os.makedirs(ll.WORD_LISTS_DIR, exist_ok=True)
    # Always create user-specific file, not generic
    path = ll.word_list_path_user_specific(user, lang)
    created = False
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([], f, indent=2, ensure_ascii=False)
        created = True
    conn = ll.get_connection()
    ll.ensure_word_table(conn, user, lang)
    ll.ensure_sessions_table(conn, user)
    conn.commit()
    conn.close()
    return created, path


# --- HTTP server ---
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "TartarusWeb/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, filename, content_type):
        path = os.path.join(WEB_DIR, filename)
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, max-age=0')
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            return self._send_static(filename, content_type)

        if parsed.path == '/api/wordlists':
            return self._send_json({'wordlists': list_word_lists()})

        if parsed.path == '/api/report':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [None])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                return self._send_json({'reports': report_data(user, lang)})
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/report/summary':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                summary = user_summary_data(user)
                if summary is None:
                    return self._send_json({'summary': None})
                return self._send_json({'summary': summary})
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/user/progress':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            category = qs.get('category', [''])[0] or None
            level = qs.get('level', [''])[0] or None
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            return self._send_json({'lists': user_progress_data(user, category, level)})

        if parsed.path == '/api/wordlist':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                return self._send_json({'words': load_word_list(user, lang)})
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/wordlist/stats':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            due_today = qs.get('due_today', ['false'])[0].lower() == 'true'
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                words = word_list_stats(user, lang, due_today_only=due_today)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            if words is None:
                return self._send_json({'error': 'no such word list'}, 404)
            return self._send_json({'words': words})

        if parsed.path == '/api/dashboard':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0] or None
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                return self._send_json(dashboard_data(user, lang))
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/wordlist/leitner':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                stats = leitner_stats_data(user, lang)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            if stats is None:
                return self._send_json({'error': 'no such word list'}, 404)
            return self._send_json({'leitner': stats})

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            return self._send_json({'error': 'invalid JSON body'}, 400)

        if parsed.path == '/api/tts':
            text = str(payload.get('text', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            wpm = payload.get('wpm', 128)
            try:
                wpm = int(wpm)
            except (TypeError, ValueError):
                wpm = 128
            if text:
                ll.speak(text, lang or None, block=True, wpm=wpm)
            return self._send_json({})

        if parsed.path == '/api/init':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            try:
                created, path = init_word_list(user, lang)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'created': created, 'path': path})

        if parsed.path == '/api/practice/start':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            audio_lang = str(payload.get('audio_lang', '')).strip() or None
            drill_all = bool(payload.get('drill_all', False))
            drill_mode = bool(payload.get('drill_mode', False))
            known_drill_mode = bool(payload.get('known_drill_mode', False))
            instant_drill = bool(payload.get('instant_drill', False))
            fast_mode = bool(payload.get('fast_mode', False))
            try:
                wpm = int(payload.get('wpm', 128))
            except (TypeError, ValueError):
                wpm = 128
            try:
                session_id, session = start_session(
                    user, lang,
                    audio_lang=audio_lang,
                    drill_all=drill_all,
                    drill_mode=drill_mode,
                    known_drill_mode=known_drill_mode,
                    instant_drill=instant_drill,
                    fast_mode=fast_mode,
                    wpm=wpm,
                )
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)
            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang': session['lang'],
                'fast_mode': session['fast_mode'],
                'progress': {
                    'correct': 0,
                    'drilled': 0,
                    'total': session['total'],
                    'questions': 0,
                    'max_questions': session['max_questions'],
                },
                'question': question,
            })

        if parsed.path == '/api/wordlist':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            words = payload.get('words', [])
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                path, count = save_word_list(user, lang, words)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'saved': True, 'path': path, 'count': count})

        if parsed.path == '/api/practice/answer':
            session_id = payload.get('session_id')
            session = SESSIONS.get(session_id)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            try:
                result = process_answer(session, payload.get('answer', ''))
            except Exception:
                SESSIONS.pop(session_id, None)
                if session.get('practiced', 0) > 0:
                    finalize_session(session, ended_early=True)
                return self._send_json({'error': 'Internal error processing answer'}, 500)
            if result.get('done'):
                SESSIONS.pop(session_id, None)
            return self._send_json(result)

        self.send_error(404)


def main():
    try:
        httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"Error: port {PORT} is already in use.")
            print(f"  Another Tartarus web server (or another process) is "
                  f"probably already listening on http://{HOST}:{PORT}/.")
            print(f"  Find it with: lsof -i :{PORT}")
            print(f"  Stop it with: kill <PID>")
            sys.exit(1)
        raise
    db_path = os.path.abspath(ll.DATABASE_FILE)
    print("Tartarus web server starting...")
    print(f"  Listening on : http://{HOST}:{PORT}/")
    print(f"  Database     : {db_path}")
    print("  Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        httpd.server_close()
        print("Server stopped.")


if __name__ == '__main__':
    main()
