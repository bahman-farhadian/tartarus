# -*- coding: utf-8 -*-
"""
Mashq web server: a localhost-only JSON API + static frontend that wraps
the same SQLite-backed scoring logic as the mashq.py CLI. Standard
library only - no extra packages needed.

Run via: ./mashq_web.sh   (serves http://127.0.0.1:9999)
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

from datetime import date, timedelta
import mashq as ll

HOST = '127.0.0.1'
PORT = 9999

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}

# Maps Mashq's locale prefixes to BCP-47 tags the Web Speech API
# recognizes more reliably for voice selection.
SPEECH_LOCALES = {
    'en': 'en-US', 'de': 'de-DE', 'fr': 'fr-FR', 'es': 'es-ES', 'it': 'it-IT',
    'nl': 'nl-NL', 'pt': 'pt-PT', 'ru': 'ru-RU', 'ja': 'ja-JP', 'zh': 'zh-CN',
    'ko': 'ko-KR', 'tr': 'tr-TR', 'pl': 'pl-PL', 'sv': 'sv-SE', 'no': 'nb-NO',
    'da': 'da-DK', 'ar': 'ar-SA',
}

def _resolve_locale(lang):
    """Map a lang name (e.g. 'kanji_a1') to a locale prefix (e.g. 'ja'),
    trying exact match first, then the first underscore-segment."""
    lang_lower = lang.lower()
    return ll.LANGUAGE_LOCALES.get(lang_lower) or ll.LANGUAGE_LOCALES.get(lang_lower.split('_')[0], '')

DRILL_TARGET = 9

# In-memory practice sessions, keyed by a random session id. Lost on
# restart, which is fine - sessions are short-lived and progress is only
# persisted to the database when a word is answered or the session ends.
SESSIONS = {}


# --- Helpers shared with the per-word question handlers in mashq.py ---
def gauge_dots(score):
    if score >= 9:
        return '●●●'
    if score >= 7:
        return '●●○'
    if score >= 4:
        return '●○○'
    return '○○○'


def gender_class(word_text):
    text_lower = word_text.lower()
    if text_lower.startswith("der "):
        return 'masc'
    if text_lower.startswith("die "):
        return 'fem'
    if text_lower.startswith("das "):
        return 'neut'
    return 'none'


def build_question(session, word_id, word_text, definition, score, leitner_box=1):
    band = ll.score_band(score)
    has_def = bool(definition)
    question = {
        'word_id': word_id,
        'word': word_text,
        'definition': definition.split('\n') if definition else [],
        'score': round(score, 1),
        'gauge': gauge_dots(score),
        'band': band,
        'gender': gender_class(word_text),
    }
    initial_drill = None
    if band == 1:
        question['type'] = 'learning' if has_def else 'spelling'
    elif band == 2:
        question['type'] = 'audio'
    else:
        # Band 3: definition + audio → type the word (no more MCQ).
        question['type'] = 'production'

    if session.get('drill_mode'):
        # Drill mode: every word requires 9 correct in a row, regardless of band.
        initial_drill = {'correct_in_a_row': 0, 'repetition': 1}
        question['drill_start'] = {
            'word': word_text,
            'definition': definition.split('\n') if definition else [],
            'repetition': 1,
            'correct_in_a_row': 0,
            'target': DRILL_TARGET,
        }

    session['current'] = {
        'word_id': word_id,
        'word_text': word_text,
        'definition': definition,
        'score': score,
        'leitner_box': leitner_box,
        'type': question['type'],
        'drill': initial_drill,
    }
    return question


MAX_QUESTIONS = ll.MAX_QUESTIONS
DRILL_WORDS = ll.DRILL_WORDS


# --- Session lifecycle ---
def start_session(user, lang, audio_lang=None, drill_mode=False):
    ll.sync_word_list(user, lang)
    words = ll.get_words_for_practice(user, lang, DRILL_WORDS if drill_mode else MAX_QUESTIONS, drill_mode=drill_mode)
    voice_lang = audio_lang or lang

    queue = [
        {'word_id': r[0], 'word_text': r[1], 'definition': r[2], 'score': r[3], 'leitner_box': r[4]}
        for r in words
    ]

    session_id = uuid.uuid4().hex
    session = {
        'user': user,
        'lang': lang,
        'lang_locale': SPEECH_LOCALES.get(_resolve_locale(voice_lang), ''),
        'queue': queue,
        'total': len(queue),
        'practiced': 0,
        'max_questions': MAX_QUESTIONS,
        'drill_mode': drill_mode,
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
    return build_question(session, entry['word_id'], entry['word_text'],
                          entry['definition'], entry['score'], entry['leitner_box'])


def finalize_session(session, ended_early=False):
    elapsed = int(time.time() - session['start_time'])
    if session['practiced'] > 0:
        ll.log_session(
            session['user'], session['lang'], elapsed, session['practiced'],
            session['correct'], len(session['incorrect']), session['drilled']
        )
    return {
        'practiced': session['practiced'],
        'correct': session['correct'],
        'incorrect': session['incorrect'],
        'drilled': session['drilled'],
        'elapsed_seconds': elapsed,
        'ended_early': ended_early,
    }


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
            if session.get('drill_mode'):
                ll.record_as_drilled(session['user'], session['lang'], cur['word_id'])
                return advance(session, 'drilled', "Drill complete.")
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'drilled')
            return advance(session, 'drilled', "Drill complete. Score set to 5.0.")
        correct = True
    else:
        drill['correct_in_a_row'] = 0
        correct = False

    drill['repetition'] += 1
    return {
        'result': 'drill_progress',
        'done': False,
        'drill': {
            'word': cur['word_text'],
            'definition': cur['definition'].split('\n') if cur['definition'] else [],
            'repetition': drill['repetition'],
            'correct_in_a_row': drill['correct_in_a_row'],
            'target': DRILL_TARGET,
            'correct': correct,
        },
    }


def process_answer(session, answer):
    answer = (answer or '').strip()
    cur = session['current']

    if cur['drill'] is not None:
        return process_drill_answer(session, answer)

    if answer == '!!':
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if answer.startswith('$'):
        cur['drill'] = {'correct_in_a_row': 0, 'repetition': 1}
        return {
            'result': 'drill_start',
            'done': False,
            'drill': {
                'word': cur['word_text'],
                'definition': cur['definition'].split('\n') if cur['definition'] else [],
                'repetition': 1,
                'correct_in_a_row': 0,
                'target': DRILL_TARGET,
            },
        }

    if answer.startswith('@'):
        if not session.get('drill_mode'):
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'mastered')
        return advance(session, 'mastered', f"Marked '{cur['word_text']}' as known.")

    if answer.startswith('!'):
        if not session.get('drill_mode'):
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'flagged')
        return advance(session, 'flagged', f"Flagged '{cur['word_text']}' for more practice.")

    correct = ll.answer_matches(answer, cur['word_text'])

    if session.get('drill_mode'):
        ll.record_as_drilled(session['user'], session['lang'], cur['word_id'])
        msg = None if correct else f"Incorrect. The word was: {cur['word_text']}"
        return advance(session, 'correct' if correct else 'incorrect', msg, attempt=answer)

    if correct:
        ll.update_word_score(session['user'], session['lang'], cur['word_id'],
                             'correct', cur['score'], cur['leitner_box'])
        return advance(session, 'correct', None, attempt=answer)

    ll.update_word_score(session['user'], session['lang'], cur['word_id'],
                         'incorrect', cur['score'], cur['leitner_box'])
    return advance(session, 'incorrect', f"Incorrect. The word was: {cur['word_text']}", attempt=answer)


# --- Word lists / report ---
def list_word_lists():
    if not os.path.isdir(ll.WORD_LISTS_DIR):
        return []
    result = []
    for fname in sorted(os.listdir(ll.WORD_LISTS_DIR)):
        if not fname.endswith('.json'):
            continue
        stem = fname[:-len('.json')]
        if '_' not in stem:
            continue
        user, lang = stem.split('_', 1)
        result.append({'user': user, 'lang': lang})
    return result


def report_data(user, lang=None):
    user_s = ll.sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return []

    if lang:
        languages = [ll.sanitize_name(lang, 'language')]
    else:
        cursor = conn.execute(f'SELECT DISTINCT language FROM "{table}" ORDER BY language')
        languages = [row[0] for row in cursor.fetchall()]

    reports = []
    for language in languages:
        where_clause, params = "WHERE language = ?", [language]
        query = (
            f'SELECT session_date, COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
            f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
            f'FROM "{table}" {where_clause} GROUP BY session_date ORDER BY session_date DESC'
        )
        rows = conn.execute(query, params).fetchall()
        if not rows:
            continue

        days = []
        for s_date, sessions, seconds, practiced, correct, incorrect, drilled in rows:
            days.append({
                'date': s_date, 'sessions': sessions, 'seconds': seconds,
                'practiced': practiced, 'correct': correct,
                'incorrect': incorrect or 0, 'drilled': drilled or 0,
                'avg_time': round(seconds / practiced, 1) if practiced else None,
            })

        total_query = (
            f'SELECT COUNT(id), SUM(duration_seconds), SUM(words_practiced), '
            f'SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
            f'FROM "{table}" {where_clause}'
        )
        t_sessions, t_seconds, t_practiced, t_correct, t_incorrect, t_drilled = conn.execute(total_query, params).fetchone()
        reports.append({
            'language': language,
            'days': days,
            'total': {
                'sessions': t_sessions, 'seconds': t_seconds, 'practiced': t_practiced,
                'correct': t_correct, 'incorrect': t_incorrect or 0, 'drilled': t_drilled or 0,
                'avg_time': round(t_seconds / t_practiced, 1) if t_practiced else None,
            },
        })
    conn.close()
    return reports


def user_summary_data(user):
    """Return aggregate daily stats across all languages for the user."""
    user_s = ll.sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None

    rows = conn.execute(
        f'SELECT session_date, COUNT(id), COUNT(DISTINCT language), '
        f'SUM(duration_seconds), SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}" GROUP BY session_date ORDER BY session_date DESC'
    ).fetchall()

    all_dates = [r[0] for r in conn.execute(f'SELECT session_date FROM "{table}"').fetchall()]
    current_streak, best_streak = ll.compute_streak(all_dates)

    totals = conn.execute(
        f'SELECT COUNT(id), COUNT(DISTINCT language), SUM(duration_seconds), '
        f'SUM(words_practiced), SUM(correct_count), SUM(incorrect_count) '
        f'FROM "{table}"'
    ).fetchone()
    conn.close()

    days = []
    for s_date, sessions, langs, seconds, practiced, correct, incorrect in rows:
        total_ans = (correct or 0) + (incorrect or 0)
        days.append({
            'date': s_date,
            'sessions': sessions,
            'languages': langs,
            'seconds': seconds or 0,
            'practiced': practiced or 0,
            'correct': correct or 0,
            'incorrect': incorrect or 0,
            'accuracy': round(100 * correct / total_ans, 1) if total_ans > 0 else None,
            'avg_time': round(seconds / practiced, 1) if practiced else None,
        })

    t_sessions, t_langs, t_seconds, t_practiced, t_correct, t_incorrect = totals
    t_total_ans = (t_correct or 0) + (t_incorrect or 0)
    return {
        'user': user_s,
        'streak': {'current': current_streak, 'best': best_streak},
        'days': days,
        'total': {
            'sessions': t_sessions,
            'languages': t_langs,
            'seconds': t_seconds or 0,
            'practiced': t_practiced or 0,
            'correct': t_correct or 0,
            'incorrect': t_incorrect or 0,
            'accuracy': round(100 * t_correct / t_total_ans, 1) if t_total_ans > 0 else None,
            'avg_time': round(t_seconds / t_practiced, 1) if t_practiced else None,
        },
    }


def word_list_stats(user, lang):
    table = ll.words_table_name(user, lang)
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return None
    rows = conn.execute(
        f'SELECT text, score, active, times_practiced, times_correct, times_incorrect, '
        f'times_drilled, times_flagged, times_mastered, last_practiced, leitner_box '
        f'FROM "{table}" ORDER BY active DESC, score ASC, text ASC'
    ).fetchall()
    conn.close()
    today = date.today()
    words = []
    for (text, score, active, practiced, correct, incorrect,
         drilled, flagged, mastered, last_practiced, leitner_box) in rows:
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
            'times_flagged': flagged,
            'times_mastered': mastered,
            'last_practiced': last_practiced,
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
    path = ll.word_list_path(user, lang)
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
    path = ll.word_list_path(user, lang)
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
    server_version = "MashqWeb/0.1"

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
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                words = word_list_stats(user, lang)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            if words is None:
                return self._send_json({'error': 'no such word list'}, 404)
            return self._send_json({'words': words})

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError):
            return self._send_json({'error': 'invalid JSON body'}, 400)

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
            drill_mode = bool(payload.get('drill_mode', False))
            try:
                session_id, session = start_session(user, lang, audio_lang=audio_lang, drill_mode=drill_mode)
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)
            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang_locale': session['lang_locale'],
                'progress': {
                    'correct': 0,
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
            print(f"  Another Mashq web server (or another process) is "
                  f"probably already listening on http://{HOST}:{PORT}/.")
            print(f"  Find it with: lsof -i :{PORT}")
            print(f"  Stop it with: kill <PID>")
            sys.exit(1)
        raise
    db_path = os.path.abspath(ll.DATABASE_FILE)
    print("Mashq web server starting...")
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
