# -*- coding: utf-8 -*-
"""
LexiLoop web server: a localhost-only JSON API + static frontend that wraps
the same SQLite-backed scoring logic as the lexiloop.py CLI. Standard
library only - no extra packages needed.

Run via: ./lexiloop_web.sh   (serves http://127.0.0.1:9999)
"""
import os
import json
import time
import random
import urllib.parse
import collections
import http.server
import uuid

import lexiloop as ll

HOST = '127.0.0.1'
PORT = 9999

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
}

# Maps lexiloop's locale prefixes to BCP-47 tags the Web Speech API
# recognizes more reliably for voice selection.
SPEECH_LOCALES = {
    'en': 'en-US', 'de': 'de-DE', 'fr': 'fr-FR', 'es': 'es-ES', 'it': 'it-IT',
    'nl': 'nl-NL', 'pt': 'pt-PT', 'ru': 'ru-RU', 'ja': 'ja-JP', 'zh': 'zh-CN',
    'ko': 'ko-KR', 'tr': 'tr-TR', 'pl': 'pl-PL', 'sv': 'sv-SE', 'no': 'nb-NO',
    'da': 'da-DK', 'ar': 'ar-SA',
}

DRILL_TARGET = 9

# In-memory practice sessions, keyed by a random session id. Lost on
# restart, which is fine - sessions are short-lived and progress is only
# persisted to the database when a word is answered or the session ends.
SESSIONS = {}


# --- Helpers shared with the per-word question handlers in lexiloop.py ---
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


def build_mc_options(word_text, definition, definition_pool):
    own_lines = [line.strip() for line in definition.split('\n') if line.strip()]
    correct_def = random.choice(own_lines)
    distractors = list(dict.fromkeys(
        d for w, d in definition_pool if w != word_text and d not in own_lines
    ))
    random.shuffle(distractors)
    options = [correct_def] + distractors[:3]
    random.shuffle(options)
    correct_letter = chr(ord('a') + options.index(correct_def))
    return options, correct_letter


def build_question(session, word_id, word_text, definition, score):
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
    correct_letter = None
    if band == 1:
        question['type'] = 'learning' if has_def else 'spelling'
    elif band == 2:
        question['type'] = 'audio'
    else:
        if has_def:
            options, correct_letter = build_mc_options(word_text, definition, session['definition_pool'])
            question['type'] = 'meaning'
            question['options'] = options
        else:
            question['type'] = 'audio'

    session['current'] = {
        'word_id': word_id,
        'word_text': word_text,
        'definition': definition,
        'score': score,
        'type': question['type'],
        'correct_letter': correct_letter,
        'drill': None,
    }
    return question


# --- Session lifecycle ---
def start_session(user, lang, number):
    ll.sync_word_list(user, lang)
    words = ll.get_words_for_practice(user, lang, number)

    session_id = uuid.uuid4().hex
    session = {
        'user': user,
        'lang': lang,
        'lang_locale': SPEECH_LOCALES.get(ll.LANGUAGE_LOCALES.get(lang.lower(), ''), ''),
        'queue': collections.deque(words),
        'definition_pool': ll.build_definition_pool(words),
        'total': len(words),
        'practiced': 0,
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'start_time': time.time(),
        'current': None,
    }
    SESSIONS[session_id] = session
    return session_id, session


def next_question(session):
    if not session['queue']:
        return None
    word_id, word_text, definition, score = session['queue'].popleft()
    return build_question(session, word_id, word_text, definition, score)


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
    word_text = session['current']['word_text']
    session['practiced'] += 1
    if status == 'correct':
        session['correct'] += 1
    elif status == 'incorrect':
        session['incorrect'].append({'word': word_text, 'attempt': attempt})
    elif status == 'drilled':
        session['drilled'] += 1

    result = {'result': status, 'message': message, 'word': word_text}
    nxt = next_question(session)
    if nxt is None:
        result['done'] = True
        result['session'] = finalize_session(session)
    else:
        result['done'] = False
        result['question'] = nxt
        result['progress'] = {'current': session['practiced'] + 1, 'total': session['total']}
    return result


def process_drill_answer(session, answer):
    cur = session['current']
    drill = cur['drill']
    if answer == '!!':
        return {'done': True, 'result': 'end', 'session': finalize_session(session, ended_early=True)}

    if ll.answer_matches(answer, cur['word_text']):
        drill['correct_in_a_row'] += 1
        if drill['correct_in_a_row'] >= DRILL_TARGET:
            ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'drilled')
            cur['drill'] = None
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
        ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'mastered')
        return advance(session, 'mastered', f"Marked '{cur['word_text']}' as known.")

    if answer.startswith('!'):
        ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'flagged')
        return advance(session, 'flagged', f"Flagged '{cur['word_text']}' for more practice.")

    if cur['type'] == 'meaning':
        correct = answer.lower()[:1] == cur['correct_letter']
    else:
        correct = ll.answer_matches(answer, cur['word_text'])

    if correct:
        ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'correct', cur['score'])
        return advance(session, 'correct', None, attempt=answer)
    ll.update_word_score(session['user'], session['lang'], cur['word_id'], 'incorrect', cur['score'])
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
    server_version = "LexiLoopWeb/0.2"

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
            try:
                number = int(payload.get('number', 20))
            except (TypeError, ValueError):
                number = 20
            try:
                session_id, session = start_session(user, lang, number)
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)
            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang_locale': session['lang_locale'],
                'progress': {'current': 1, 'total': session['total']},
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
            result = process_answer(session, payload.get('answer', ''))
            if result.get('done'):
                SESSIONS.pop(session_id, None)
            return self._send_json(result)

        self.send_error(404)


def main():
    httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    db_path = os.path.abspath(ll.DATABASE_FILE)
    print("LexiLoop web server starting...")
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
