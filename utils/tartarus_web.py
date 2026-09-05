# -*- coding: utf-8 -*-
"""
Tartarus web server: a localhost-only JSON API + static frontend built on
the shared SQLite-backed scoring engine in tartarus.py. Standard library
only - no extra packages needed.

Run via: make web   (serves http://127.0.0.1:9999)
"""
import os
import sys
import errno
import json
import time
import urllib.parse
import http.server
import uuid
import threading
import ipaddress
from pathlib import Path

from datetime import date
import tartarus as ll

HOST = os.environ.get('TARTARUS_HOST', '127.0.0.1')
PORT = int(os.environ.get('TARTARUS_PORT', '9999'))

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(PROJECT_DIR, 'web')

STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/style.css': ('style.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/favicon.png': ('favicon.png', 'image/png'),
}

# Browsers request these automatically on every site regardless of whether a
# <link rel="icon"> is present. Serve the same favicon for all of them
# (browsers accept a PNG here despite the .ico extension) instead of letting
# them 404 -- that was showing up as noise in the log for a request every
# browser makes unconditionally, not an actual error.
ICON_PROBE_PATHS = {'/favicon.ico', '/apple-touch-icon.png', '/apple-touch-icon-precomposed.png'}


# In-memory practice sessions, keyed by a random session id. Lost on
# restart, which is fine - sessions are short-lived and progress is only
# persisted to the database when a word is answered or the session ends.
SESSIONS = {}
SESSIONS_LOCK = threading.RLock()


def positive_environment_int(name, default):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


SESSION_TTL_SECONDS = positive_environment_int('TARTARUS_SESSION_TTL_SECONDS', 30 * 60)
MAX_ACTIVE_SESSIONS = positive_environment_int('TARTARUS_MAX_ACTIVE_SESSIONS', 100)
MAX_REQUEST_BYTES = positive_environment_int('TARTARUS_MAX_REQUEST_BYTES', 1_000_000)


def cleanup_sessions(now=None):
    """Drop expired ephemeral sessions. Corrective drills are session-local only."""
    now = time.time() if now is None else now
    with SESSIONS_LOCK:
        expired = [session_id for session_id, session in SESSIONS.items()
                   if session.get('expires_at', 0) <= now]
        for session_id in expired:
            SESSIONS.pop(session_id, None)
    return len(expired)


def register_session(session_id, session):
    cleanup_sessions()
    with SESSIONS_LOCK:
        if len(SESSIONS) >= MAX_ACTIVE_SESSIONS:
            raise ValueError('Too many active sessions. End an existing session and try again.')
        session['last_activity'] = time.time()
        session['expires_at'] = session['last_activity'] + SESSION_TTL_SECONDS
        SESSIONS[session_id] = session


MAX_QUESTIONS = ll.MAX_QUESTIONS
DRILL_TARGET = ll.DRILL_TARGET


def drill_definition_lines(current):
    """Return the definition shown while a word is being drilled."""
    prompt = (
        current.get('drill_definition')
        or current.get('prompt_definition')
        or current.get('definition')
        or ''
    )
    return prompt.split('\n') if prompt else []


# ---------------------------------------------------------------------------
# Consolidation Track session builder
# ---------------------------------------------------------------------------

def _resume_stage_info(mode, completed_day):
    """Recompute stage/stage_name/day for a resumed drill from the word's
    current persisted progress, rather than storing a second copy of it."""
    if mode == 'spaced_maintenance':
        return 5, 'Spaced Maintenance', 0
    if mode == 'encoding':
        return 0, 'Encoding', 0
    day = ll.consolidation_next_day(completed_day)
    stage, stage_name, _ = ll.consolidation_stage_for_day(day)
    return stage, stage_name, day


def _resume_pending_drill_words(user, lang, pending, today):
    """One-item selection resuming a durable drill obligation, in the same
    shape select_practice_words() returns so consolidation_start_session doesn't
    need to special-case it downstream."""
    table = ll.words_table_name(user, lang)
    conn = ll.get_connection()
    try:
        row = conn.execute(
            f'SELECT content_id,score,leitner_box,consolidation_step FROM "{table}" WHERE id=?',
            (pending['word_id'],),
        ).fetchone()
    finally:
        conn.close()
    item = None
    if row is not None:
        content_id = row[0]
        material = {i['content_id']: i for i in ll.load_practice_items(ll.word_list_path(user, lang))}
        item = material.get(content_id)
    if row is None or item is None:
        # The word was removed or the list edited since the drill started --
        # drop the orphaned obligation rather than blocking forever.
        conn = ll.get_connection()
        try:
            ll.clear_pending_drill(conn, user, lang, pending['word_id']); conn.commit()
        finally:
            conn.close()
        return None
    _, score, leitner_box, completed_day = row
    mode = pending['mode']
    stage, stage_name, day = _resume_stage_info(mode, completed_day)
    state = ll.consolidation_state_breakdown(user, lang, today)
    words = [(
        pending['word_id'], item['word'], item['definition'], score,
        leitner_box, item['word_frequency'], mode, stage, stage_name, day,
    )]
    return words, pending['context'], mode, stage, stage_name, day, state


def consolidation_start_session(user, lang, audio_lang=None):
    """Build one due-first session whose stage metadata belongs to each word.

    Resumes any durable mandatory-drill obligation before selecting
    anything else -- a browser refresh, server restart, or crash must not
    be able to lose a pending correction."""
    today = date.today().isoformat()
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    ll.sync_word_list(user, lang)

    resumed = None
    pending = ll.get_pending_drill(user, lang)
    if pending is not None:
        resumed_selection = _resume_pending_drill_words(user, lang, pending, today)
        if resumed_selection is not None:
            words, context, mode, stage, stage_name, day, state = resumed_selection
            resumed = pending
        else:
            words, context, mode, stage, stage_name, day, state = (
                ll.select_practice_words(user, lang, today)
            )
    else:
        words, context, mode, stage, stage_name, day, state = (
            ll.select_practice_words(user, lang, today)
        )
    if not words:
        if state['complete']:
            raise ValueError(
                'The Tartarus track is complete and no Leitner maintenance is ready.'
            )
        raise ValueError(
            "Today's Tartarus work is complete and no Leitner maintenance is ready."
        )

    source_language = lang.split('_', 1)[0].lower()
    voice = audio_lang or (
        source_language
        if source_language in {'english', 'german'}
        else lang
    )
    queue = [
        {
            'lang': lang,
            'word_id': row[0],
            'word_text': row[1],
            'definition': row[2],
            'score': row[3],
            'leitner_box': row[4],
            'mode': row[6],
            'stage': row[7],
            'stage_name': row[8],
            'day': row[9],
            'context': context,
        }
        for row in words
    ]
    sid = uuid.uuid4().hex
    session = {
        'user': user,
        'lang': lang,
        'voice_lang': voice,
        'queue': queue,
        'total': len(queue),
        'practiced': 0,
        'max_questions': min(MAX_QUESTIONS, len(queue)),
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'file_stats': {},
        'start_time': time.time(),
        'current': None,
        'is_scoring': True,
        'is_spaced_maintenance': context == 'spaced_maintenance',
        'is_consolidation': True,
        'learning_context': context,
        'session_modes': sorted({entry['mode'] for entry in queue}),
        'session_stage': stage,
        'lock': threading.RLock(),
        'question_sequence': 0,
        'answer_results': {},
        '_resume_drill': resumed,
    }
    register_session(sid, session)
    meta = {
        'mode': mode,
        'stage': stage,
        'stage_name': stage_name,
        'day': day,
        'remaining_tasks': state['available_tasks'],
        'is_spaced_maintenance': context == 'spaced_maintenance',
        'state': state,
    }
    ll.log_event(
        'CONSOLIDATION_SESSION_STARTED',
        user=user,
        lang=lang,
        modes=session['session_modes'],
    )
    return sid, session, meta


# ---------------------------------------------------------------------------
# Supplementary practice tracks (Encoding Practice, Reading/Listening
# Retrieval): independent of Consolidation Track / Spaced Maintenance,
# never mutate score, leitner_box, or consolidation_step. Selection is
# bucket-backed (ll.select_bucket_words) instead of due-date-driven, so
# these tracks are "endless" in two senses: each session itself has no
# fixed-size cap -- it runs the full, freshly-shuffled set of everything
# currently eligible, ending only when that set is exhausted or the
# learner cancels -- and a learner can start a new session in one of them
# as soon as the previous one ends, indefinitely.
# ---------------------------------------------------------------------------

PRACTICE_TRACK_NAMES = {
    'encoding_practice': 'Encoding Practice',
    'retrieval_reading': 'Reading Retrieval',
    'retrieval_listening': 'Listening Retrieval',
}


def bucket_start_session(user, lang, track, audio_lang=None):
    """Build a session for one supplementary, non-scoring practice track.

    Unlike consolidation_start_session, this never resumes a durable
    pending_drills obligation -- these tracks never create one; any wrong
    answer on Reading/Listening Retrieval starts a drill held only in this
    in-memory session, and Encoding Practice never drills at all."""
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    ll.sync_word_list(user, lang)
    words = ll.select_bucket_words(user, lang, track)
    if not words:
        raise ValueError(
            f"No material is available for {PRACTICE_TRACK_NAMES[track]} in this list yet."
        )

    source_language = lang.split('_', 1)[0].lower()
    voice = audio_lang or (
        source_language
        if source_language in {'english', 'german'}
        else lang
    )
    queue = [
        {
            'lang': lang,
            'word_id': row[0],
            'word_text': row[1],
            'definition': row[2],
            'score': row[3],
        }
        for row in words
    ]
    sid = uuid.uuid4().hex
    session = {
        'user': user,
        'lang': lang,
        'voice_lang': voice,
        'track': track,
        'queue': queue,
        'total': len(queue),
        'practiced': 0,
        'max_questions': len(queue),
        'correct': 0,
        'drilled': 0,
        'incorrect': [],
        'file_stats': {},
        'start_time': time.time(),
        'current': None,
        'is_scoring': False,
        'is_spaced_maintenance': False,
        'is_consolidation': False,
        'learning_context': track,
        'session_modes': [track],
        'session_stage': None,
        'lock': threading.RLock(),
        'question_sequence': 0,
        'answer_results': {},
        '_resume_drill': None,
    }
    register_session(sid, session)
    meta = {
        'mode': track,
        'track': track,
        'track_name': PRACTICE_TRACK_NAMES[track],
        'stage': None,
        'stage_name': PRACTICE_TRACK_NAMES[track],
        'day': None,
        'remaining_tasks': len(queue),
        'is_spaced_maintenance': False,
        'state': None,
    }
    ll.log_event('PRACTICE_TRACK_SESSION_STARTED', user=user, lang=lang, track=track)
    return sid, session, meta


def next_bucket_question(session):
    if not session['queue']:
        return None
    entry = session['queue'].pop(0)
    track = session['track']
    question = ll.build_question_data(
        entry['word_id'], entry['word_text'], entry['definition'], entry['score'],
    )
    full_lines = entry['definition'].split('\n') if entry['definition'] else []
    primary = ll.english_definition_only(entry['definition'])
    question['type'] = track
    question['word_unmasked'] = entry['word_text']
    if track == 'encoding_practice':
        # Always both definition lines, exactly like Encoding's own
        # presentation, regardless of band -- unlike build_question_data's
        # band<8 cutoff, which doesn't apply here. The word itself is
        # always fully visible (dim styling, not score-based masking) --
        # this track is a typing/copying exercise for initial encoding,
        # not a recall test, so there is nothing to guess.
        question['word'] = entry['word_text']
        question['definition'] = full_lines
    elif track == 'retrieval_reading':
        question['word'] = ''
        question['definition'] = [primary] if primary else []
    else:  # retrieval_listening: audio only, no text or definition at all.
        question['word'] = ''
        question['definition'] = []
        question['text_hidden'] = True
    question['consolidation'] = {
        'mode': track, 'stage': None,
        'stage_name': PRACTICE_TRACK_NAMES[track], 'day': None,
    }
    session['current'] = {
        'lang': entry['lang'],
        'word_id': entry['word_id'],
        'word_text': entry['word_text'],
        'definition': entry['definition'],
        'prompt_definition': '\n'.join(question.get('definition', [])),
        'drill_definition': '\n'.join(full_lines),
        'score': entry['score'],
        'type': track,
        'track': track,
        'drill': None,
        'revealed': False,
        'started_at': time.time(),
    }
    session['question_sequence'] += 1
    qid = uuid.uuid4().hex
    session['current']['question_id'] = qid
    session['current']['sequence'] = session['question_sequence']
    question['question_id'] = qid
    question['sequence'] = session['question_sequence']
    return question


def process_bucket_answer(session, answer, *, timed_out=False):
    """All three supplementary tracks share one mechanic: correct advances,
    wrong repeats the same question -- no drill, ever, since they're
    optional practice, not the mandatory track.

    Encoding Practice is already fully visible from the start, so a miss
    there just means "try typing it again." Reading/Listening Retrieval
    start hidden (that's the recall test); a blind guess after a miss
    isn't productive, so the first miss on either immediately reveals the
    word (full Encoding-style presentation: unmasked, both definition
    lines) instead of leaving the learner to keep guessing blind --
    further attempts are then a guaranteed-achievable copy, not more
    guesswork."""
    cur = session['current']; answer = '' if answer is None else str(answer)
    record_current_time(session)
    correct = False if timed_out else ll.answer_matches(answer, cur['word_text'])
    if correct:
        return advance(session, 'correct', None, attempt=answer)
    session['incorrect'].append({'word': cur['word_text'], 'attempt': answer})
    record_file_incorrect(session)
    cur['started_at'] = time.time()
    if session['track'] == 'encoding_practice' or cur.get('revealed'):
        return {'result': 'retry', 'done': False, 'message': 'Not quite. Try again.'}
    cur['revealed'] = True
    full_lines = cur['definition'].split('\n') if cur['definition'] else []
    return {
        'result': 'retry', 'done': False,
        'message': "Not quite -- here it is. Type it once to lock it in.",
        'reveal': {'word': cur['word_text'], 'definition': full_lines},
    }


# --- Session lifecycle ---
def next_question(session):
    if not session.get('is_scoring', True):
        return next_bucket_question(session)
    if not session['queue']:
        return None
    entry = session['queue'].pop(0)
    question = ll.build_question_data(
        entry['word_id'],
        entry['word_text'],
        entry['definition'],
        entry['score'],
    )
    full_lines = (
        entry['definition'].split('\n')
        if isinstance(entry['definition'], str)
        else (entry['definition'] or [])
    )
    mode = entry['mode']
    resume = session.pop('_resume_drill', None)
    drill = None
    drill_target = (
        ll.EFFORTFUL_RETRIEVAL_DRILL_TARGET
        if entry['context'] == 'consolidation' and mode == 'effortful_retrieval'
        else DRILL_TARGET
    )
    if resume is not None:
        # Restore the exact persisted streak -- this question is already
        # mid-drill, not starting fresh, so no new pending_drills row.
        drill_target = resume['target']
        drill = {
            'correct_in_a_row': resume['correct_in_a_row'],
            'repetition': resume['correct_in_a_row'] + 1,
            'target': drill_target,
            # Effortful Retrieval's own 2-in-a-row check-in is the recall task itself
            # (README: "target hidden") -- not punishment, so it stays
            # hidden until/unless a miss escalates it to the real
            # corrective drill (target jumps from 2 to DRILL_TARGET).
            # Every other mode only ever reaches "drill" via an actual
            # mistake, so its target is always DRILL_TARGET already.
            'show_word': drill_target != ll.EFFORTFUL_RETRIEVAL_DRILL_TARGET,
        }
        question['drill_start'] = dict(drill)
    elif entry['context'] == 'consolidation' and mode == 'effortful_retrieval':
        drill = {
            'correct_in_a_row': 0,
            'repetition': 1,
            'target': drill_target,
            'show_word': False,
        }
        question['drill_start'] = dict(drill)
        conn = ll.get_connection()
        try:
            ll.start_pending_drill(conn, session['user'], session['lang'], entry['word_id'], drill_target, entry['context'], mode)
            conn.commit()
        finally:
            conn.close()
    if mode in ('cued_recall', 'effortful_retrieval', 'free_recall', 'reconsolidation', 'automaticity'):
        question['type'] = mode
        question['word_unmasked'] = entry['word_text']
        # The word is masked or fully hidden in every one of these stages, so
        # the example-sentence line (which always embeds the literal target
        # word) is withheld -- only the primary meaning line is shown.
        primary = ll.english_definition_only('\n'.join(full_lines))
        question['definition'] = [primary] if primary else []
        if mode == 'cued_recall':
            vowels = 'aeiouAEIOUäöüÄÖÜ'
            question['word'] = ''.join(
                '_' if character in vowels else character
                for character in entry['word_text']
            )
        else:
            question['word'] = ''
    elif mode == 'spaced_maintenance':
        question['type'] = 'spaced_maintenance'
        question['word'] = ''
        question['word_unmasked'] = entry['word_text']
        # Same anti-cheat rule as the daily stages above: word is hidden, so
        # withhold the example-sentence line.
        primary = ll.english_definition_only('\n'.join(full_lines))
        question['definition'] = [primary] if primary else []
    if drill is not None:
        question['drill_start'].update({
            'word': entry['word_text'],
            'definition': question.get('definition', []),
        })
    question['consolidation'] = {
        'mode': mode,
        'stage': entry['stage'],
        'stage_name': entry['stage_name'],
        'day': entry['day'],
    }
    session['current'] = {
        'lang': entry.get('lang', session['lang']),
        'word_id': entry['word_id'],
        'word_text': entry['word_text'],
        'definition': entry['definition'],
        'prompt_definition': '\n'.join(question.get('definition', [])),
        # Deliberately the full definition, not the (possibly trimmed)
        # question['definition'] -- once a mistake escalates to the
        # corrective drill, show_word reveals the raw word anyway, so there
        # is no cheat risk left, and the example sentence is helpful there.
        'drill_definition': '\n'.join(full_lines),
        'score': entry['score'],
        'leitner_box': entry['leitner_box'],
        'type': question['type'],
        'mode': mode,
        'stage': entry['stage'],
        'stage_name': entry['stage_name'],
        'day': entry['day'],
        'context': entry['context'],
        'drill_target': drill_target,
        'drill': drill,
        'started_at': time.time(),
    }
    session['question_sequence'] += 1
    qid = uuid.uuid4().hex
    session['current']['question_id'] = qid
    session['current']['sequence'] = session['question_sequence']
    question['question_id'] = qid
    question['sequence'] = session['question_sequence']
    return question



def record_current_time(session):
    """Assign the current word's elapsed time to its source file."""
    current = session.get('current')
    if not current:
        return
    now = time.time()
    started_at = current.get('started_at', now)
    elapsed = max(0.0, now - started_at)
    lang = current.get('lang', session['lang'])
    stats = session['file_stats'].setdefault(lang, {
        'seconds': 0.0, 'practiced': 0, 'correct': 0,
        'incorrect': 0, 'drilled': 0,
    })
    stats['seconds'] += elapsed
    current['started_at'] = now


def record_file_result(session, status, lang=None):
    """Record a completed result against the word's source file."""
    lang = lang or session['current'].get('lang', session['lang'])
    stats = session['file_stats'].setdefault(lang, {
        'seconds': 0.0, 'practiced': 0, 'correct': 0,
        'incorrect': 0, 'drilled': 0,
    })
    stats['practiced'] += 1
    if status == 'correct':
        stats['correct'] += 1
    elif status == 'incorrect':
        stats['incorrect'] += 1
    elif status == 'drilled':
        stats['drilled'] += 1


def record_file_incorrect(session, lang=None):
    """Record a wrong attempt without marking the word completed."""
    lang = lang or session['current'].get('lang', session['lang'])
    stats = session['file_stats'].setdefault(lang, {
        'seconds': 0.0, 'practiced': 0, 'correct': 0,
        'incorrect': 0, 'drilled': 0,
    })
    stats['incorrect'] += 1


def finalize_session(session, ended_early=False):
    record_current_time(session); elapsed=int(time.time()-session['start_time'])
    modes=session.get('session_modes',[])
    mode=modes[0] if len(modes)==1 else None
    if session['practiced']>0:
        ll.log_session(session['user'],session['lang'],elapsed,session['practiced'],session['correct'],len(session['incorrect']),session['drilled'],mode=mode,stage=session.get('session_stage'))
    practiced=session['practiced']
    # First-attempt accuracy: correct / (correct + incorrect). The same
    # formula used everywhere else (user report, dashboard) -- a completed
    # drill counts toward "practiced" but is deliberately excluded here
    # since it wasn't a first-attempt correct answer.
    attempted=session['correct']+len(session['incorrect'])
    return {
        'practiced':practiced,'correct':session['correct'],'incorrect':session['incorrect'],'drilled':session['drilled'],
        'elapsed_seconds':elapsed,'ended_early':ended_early,'accuracy':round(100*session['correct']/attempted,1) if attempted else None,
        'avg_seconds_per_item':round(elapsed/practiced,1) if practiced else None,
        'consolidation':{'modes':modes,'voided':ended_early},
    }




def advance(session, status, message, attempt=None):
    cur=session['current']; session['practiced']+=1
    if status=='correct': session['correct']+=1
    elif status=='drilled': session['drilled']+=1
    record_file_result(session,status)
    result={'result':status,'message':message,'word':cur['word_text']}
    nxt=None if session['practiced']>=session['max_questions'] else next_question(session)
    if nxt is None:
        result['done']=True; result['session']=finalize_session(session)
    else:
        result['done']=False; result['question']=nxt; result['progress']={'correct':session['correct'],'drilled':session['drilled'],'total':session['total'],'questions':session['practiced'],'max_questions':session['max_questions']}
    return result



def process_drill_answer(session, answer):
    cur=session['current']; drill=cur['drill']; target=drill.get('target',DRILL_TARGET)
    correct=ll.answer_matches(answer,cur['word_text'])
    if not correct and target < DRILL_TARGET:
        target=DRILL_TARGET; drill['target']=target
        ll.record_consolidation_answer(session['user'],session['lang'],cur['word_id'],False)
        session['incorrect'].append({'word':cur['word_text'],'attempt':answer}); record_file_incorrect(session)
    drill['correct_in_a_row']=drill['correct_in_a_row']+1 if correct else 0
    if drill['correct_in_a_row']>=target:
        cur['drill']=None
        if cur['context']=='spaced_maintenance': ll.complete_maintenance_drill(session['user'],session['lang'],cur['word_id'])
        else: ll.complete_consolidation_drill(session['user'],session['lang'],cur['word_id'])
        conn=ll.get_connection()
        try: ll.clear_pending_drill(conn,session['user'],session['lang'],cur['word_id']); conn.commit()
        finally: conn.close()
        result=advance(session,'drilled','Drill complete.')
        result['drill']={'word':cur['word_text'],'definition':drill_definition_lines(cur),'repetition':drill['repetition'],'correct_in_a_row':target,'target':target,'correct':True,'show_word':True}
        return result
    drill['repetition']+=1
    conn=ll.get_connection()
    try:
        ll.update_pending_drill_progress(conn,session['user'],session['lang'],cur['word_id'],drill['correct_in_a_row'],target=target)
        conn.commit()
    finally: conn.close()
    # Still on Effortful Retrieval's own native check-in (target hasn't escalated past
    # EFFORTFUL_RETRIEVAL_DRILL_TARGET) -- that's the recall task itself, stays hidden.
    # Any other target value only exists because a real mistake escalated
    # it, which is corrective punishment and must stay visible.
    show_word = target != ll.EFFORTFUL_RETRIEVAL_DRILL_TARGET
    return {'result':'drill_progress','done':False,'drill':{'word':cur['word_text'],'definition':drill_definition_lines(cur),'repetition':drill['repetition'],'correct_in_a_row':drill['correct_in_a_row'],'target':target,'correct':correct,'show_word':show_word}}



def process_answer(session, answer, *, timed_out=False):
    """Evaluate learning text; timer expiry is an explicit transport event."""
    if not session.get('is_scoring', True):
        return process_bucket_answer(session, answer, timed_out=timed_out)
    cur=session['current']; answer='' if answer is None else str(answer); record_current_time(session)
    if cur['drill'] is not None:
        return process_drill_answer(session, answer)
    correct=False if timed_out else ll.answer_matches(answer,cur['word_text'])
    if cur['context']=='spaced_maintenance':
        ll.record_maintenance_answer(session['user'],session['lang'],cur['word_id'],correct)
    else:
        ll.record_consolidation_answer(session['user'],session['lang'],cur['word_id'],correct)
    if correct:
        return advance(session,'correct',None,attempt=answer)
    session['incorrect'].append({'word':cur['word_text'],'attempt':answer}); record_file_incorrect(session)
    cur['drill']={'correct_in_a_row':0,'repetition':1,'target':cur['drill_target'],'show_word':True}
    conn=ll.get_connection()
    try:
        ll.start_pending_drill(conn,session['user'],session['lang'],cur['word_id'],cur['drill']['target'],cur['context'],cur['mode'])
        conn.commit()
    finally: conn.close()
    return {'result':'drill_start','done':False,'message':'Incorrect. Complete the mandatory drill before continuing.','drill':{'word':cur['word_text'],'definition':drill_definition_lines(cur),'repetition':1,'correct_in_a_row':0,'target':cur['drill']['target'],'correct':False,'show_word':True}}



# --- Word lists / report ---
def _list_descriptor(user, lang, path, data, shared, owner=None):
    """Build the one list-descriptor shape consumed by every API view."""
    metadata = data['metadata']
    language = str(metadata.get('language', 'unknown')).lower()
    kind = str(metadata.get('type', metadata.get('kind', 'vocabulary'))).lower()
    kind = 'sentences' if kind == 'sentences' else 'vocabulary'
    level = str(metadata.get('cefr_level', metadata.get('level', 'all'))).lower()
    pos = str(metadata.get('pos', '')).lower()
    if not pos:
        parts = Path(path).stem.split('_')
        known_pos = {'noun', 'verb', 'adjective', 'adverb', 'pronoun', 'preposition', 'conjunction', 'interjection'}
        pos = next((part for part in parts if part in known_pos), 'all')
    return {
        'user': user,
        'owner': owner,
        'lang': lang,
        'language': language,
        'kind': kind,
        'category': f'{language}_{kind}',
        'cefr_level': level,
        'pos': pos,
        'name': str(metadata.get('name', lang)),
        'word_count': len(data['items']),
        'ordered': bool(metadata.get('ordered', False)),
        'shared': shared,
    }


def list_word_lists():
    """Discover shared JSON material and user-owned root lists without leakage."""
    root = Path(ll.WORD_LISTS_DIR)
    if not root.is_dir():
        return []
    conn = ll.get_connection()
    users = [row[0] for row in conn.execute(
        "SELECT name FROM users WHERE name != 'system' ORDER BY name"
    )]
    conn.close()

    personal = []
    shared_paths = []
    for path in sorted(root.rglob('*.json')):
        owner = ll.personal_list_owner(path.stem, users) if path.parent == root else None
        if owner:
            personal.append((path, owner, path.stem[len(owner) + 1:]))
        else:
            shared_paths.append(path)

    personal_by_owner = {}
    for path, owner, lang in personal:
        try:
            data = ll.read_word_list(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        personal_by_owner.setdefault(owner, {})[lang] = _list_descriptor(
            owner, lang, path, data, shared=False, owner=owner,
        )

    shared = []
    for path in shared_paths:
        try:
            shared.append((path, path.stem, ll.read_word_list(path)))
        except (OSError, ValueError, json.JSONDecodeError):
            continue

    descriptors = []
    for user in users:
        overrides = personal_by_owner.get(user, {})
        for path, lang, data in shared:
            if lang not in overrides:
                descriptors.append(_list_descriptor(user, lang, path, data, shared=True))
        descriptors.extend(overrides.values())
    return sorted(descriptors, key=lambda item: (item['user'], item['category'], item['cefr_level'], item['pos'], item['lang']))

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


def today_practice_overview(user, today=None):
    """Per (file, mode) breakdown of what a user practiced today."""
    today = today or date.today().isoformat()
    user_s = ll.sanitize_name(user, 'user')
    table = f"sessions_{user_s}"
    conn = ll.get_connection()
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table,))
    if cursor.fetchone() is None:
        conn.close()
        return []
    rows = conn.execute(
        f'SELECT language, mode, stage, COUNT(id), SUM(duration_seconds), '
        f'SUM(words_practiced), SUM(correct_count), SUM(incorrect_count), SUM(drilled_count) '
        f'FROM "{table}" WHERE session_date = ? '
        f'GROUP BY language, mode, stage ORDER BY language, MIN(id)',
        (today,),
    ).fetchall()
    conn.close()

    mode_names = {mode_key: name for _, _, _, name, mode_key in ll.CONSOLIDATION_STAGE_MAP}
    mode_names.update(PRACTICE_TRACK_NAMES)
    mode_names['spaced_maintenance'] = 'Spaced Maintenance'
    daily_stages = ('encoding', 'cued_recall', 'effortful_retrieval', 'free_recall', 'reconsolidation', 'automaticity')

    entries = []
    for language, mode, stage, sessions, seconds, practiced, correct, incorrect, drilled in rows:
        label = mode_names.get(mode, mode or 'Practice')
        if stage is not None and mode in daily_stages:
            label = f'{label} · Day {stage}'
        entries.append({
            'language': language, 'mode': mode, 'mode_name': label,
            'sessions': sessions, 'seconds': seconds or 0, 'practiced': practiced or 0,
            'correct': correct or 0, 'incorrect': incorrect or 0, 'drilled': drilled or 0,
        })
    return entries


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


def user_progress_data(user, category=None, level=None, lang=None):
    user_s=ll.sanitize_name(user,'user'); conn=ll.get_connection(); results=[]
    lang_s=ll.sanitize_name(lang,'language') if lang else None
    try:
        for item in list_word_lists():
            if item['user']!=user_s: continue
            if category and item['category']!=category: continue
            if level and item['cefr_level']!=level: continue
            if lang_s and item['lang']!=lang_s: continue
            table=ll.words_table_name(user_s,item['lang'])
            if not ll.table_exists(conn,table):
                total=item['word_count']; consolidation_score9=box10=0
            else:
                total,consolidation_score9,box10=conn.execute(
                    f'SELECT COUNT(*),SUM(CASE WHEN score>=9 THEN 1 ELSE 0 END),SUM(CASE WHEN score>=9 AND leitner_box=10 THEN 1 ELSE 0 END) FROM "{table}" WHERE active=1'
                ).fetchone(); consolidation_score9=consolidation_score9 or 0; box10=box10 or 0
            state=ll.consolidation_state_breakdown(user_s,item['lang'],conn=conn); consolidation_complete=state['complete']
            results.append({**item,'total':total or 0,'consolidation_score9':consolidation_score9,'leitner_box10':box10,'consolidation_track_complete':consolidation_complete,'learning_complete':bool(consolidation_complete and total and box10==total),'consolidation':state})
        return results
    finally: conn.close()



def leitner_stats_data(user, lang):
    user_s=ll.sanitize_name(user,'user'); lang_s=ll.sanitize_name(lang,'language'); table=ll.words_table_name(user_s,lang_s); conn=ll.get_connection()
    try:
        if not ll.table_exists(conn,table): return None
        distribution={str(i):0 for i in range(1,11)}
        for box,count in conn.execute(f'SELECT leitner_box,COUNT(*) FROM "{table}" WHERE active=1 AND score>=9 AND leitner_box IS NOT NULL GROUP BY leitner_box'):
            distribution[str(box)]=count
        return {'distribution':distribution,'ready':len(ll.maintenance_ready_words(user_s,lang_s)),'box10':distribution['10']}
    finally: conn.close()


def _consolidation_roadmap_payload(state):
    """Return the canonical per-cohort Consolidation Track roadmap shape."""
    return dict(state)



def trend_data(user, lang, metric):
    """Return cumulative append-only transition history without mutating schema."""
    user_s=ll.sanitize_name(user,'user'); lang_s=ll.sanitize_name(lang,'language')
    if metric not in ll.MASTERY_EVENT_TYPES:
        raise ValueError("'metric' must be 'mastered' or 'box10'.")
    conn=ll.get_connection()
    try:
        if not ll.table_exists(conn,'mastery_events'):
            return []
        rows=conn.execute(
            'SELECT mastered_date,COUNT(*) FROM mastery_events '
            'WHERE user=? AND lang=? AND event_type=? GROUP BY mastered_date ORDER BY mastered_date',
            (user_s,lang_s,metric),
        ).fetchall()
    finally: conn.close()
    cumulative=0; series=[]
    for event_date,count in rows:
        cumulative+=count
        series.append({'date':str(event_date)[:10],'cumulative':cumulative})
    return series


def dashboard_data(user, lang=None):
    """Return factual, read-only session and track metrics."""
    user_s=ll.sanitize_name(user,'user'); lang_s=ll.sanitize_name(lang,'language') if lang else None; conn=ll.get_connection()
    try:
        stable=ll.sessions_table_name(user_s); total_seconds=total_practiced=total_correct=total_incorrect=0; dates=[]
        if ll.table_exists(conn,stable):
            where='WHERE language=?' if lang_s else ''; params=(lang_s,) if lang_s else ()
            row=conn.execute(f'SELECT COALESCE(SUM(duration_seconds),0),COALESCE(SUM(words_practiced),0),COALESCE(SUM(correct_count),0),COALESCE(SUM(incorrect_count),0) FROM "{stable}" {where}',params).fetchone(); total_seconds,total_practiced,total_correct,total_incorrect=row
            dates=[r[0] for r in conn.execute(f'SELECT session_date FROM "{stable}" {where}',params)]
        current_streak,best_streak=ll.compute_streak(dates); total_answers=total_correct+total_incorrect
        result={'overview':{'streak':{'current':current_streak,'best':best_streak},'total_seconds':total_seconds,'overall_accuracy':round(100*total_correct/total_answers,1) if total_answers else None},'velocity':{'avg_seconds_per_word':round(total_seconds/total_practiced,1) if total_practiced else None,'sessions':len(dates)},'tracks':None,'nemesis':None,'roadmap':None}
        if lang_s:
            table=ll.words_table_name(user_s,lang_s)
            if ll.table_exists(conn,table):
                total,tmaster,box10=conn.execute(f'SELECT COUNT(*),SUM(CASE WHEN score>=9 THEN 1 ELSE 0 END),SUM(CASE WHEN score>=9 AND leitner_box=10 THEN 1 ELSE 0 END) FROM "{table}" WHERE active=1').fetchone(); tmaster=tmaster or 0; box10=box10 or 0
                state=ll.consolidation_state_breakdown(user_s,lang_s,conn=conn); tcomplete=state['complete']
                result['tracks']={'total':total or 0,'consolidation_score9':tmaster,'leitner_box10':box10,'consolidation_track_complete':tcomplete,'learning_complete':bool(tcomplete and total and box10==total),'consolidation':state}
                try: material={i['content_id']:i for i in ll.load_practice_items(ll.word_list_path(user_s,lang_s))}
                except Exception: material={}
                result['nemesis']=[{'word':material.get(cid,{}).get('word',cid),'times_incorrect':wrong,'times_correct':right,'score':round(score,1)} for cid,wrong,right,score in conn.execute(f'SELECT content_id,times_incorrect,times_correct,score FROM "{table}" WHERE active=1 AND times_incorrect>0 ORDER BY times_incorrect DESC,score ASC LIMIT 10')]
                distribution={str(i):0 for i in range(1,11)}
                for box,count in conn.execute(f'SELECT leitner_box,COUNT(*) FROM "{table}" WHERE active=1 AND leitner_box IS NOT NULL GROUP BY leitner_box'):
                    distribution[str(box)]=count
                consolidation_roadmap=_consolidation_roadmap_payload(state)
                result['roadmap']={'consolidation':consolidation_roadmap,'leitner_distribution':distribution,'maintenance_ready':len(ll.maintenance_ready_words(user_s,lang_s))}
        return result
    finally: conn.close()



def word_list_stats(user, lang):
    """Return factual per-item progress without synchronizing or mutating state."""
    user_s = ll.sanitize_name(user, 'user')
    lang_s = ll.sanitize_name(lang, 'language')
    table = ll.words_table_name(user_s, lang_s)
    conn = ll.get_connection()
    try:
        if not ll.table_exists(conn, table):
            return None
        material = {item['content_id']: item for item in ll.load_practice_items(ll.word_list_path(user_s, lang_s))}
        ready_ids = {row[0] for row in ll.maintenance_ready_words(user_s, lang_s, num_words=10**9)}
        reinforcement = {
            row['id']: row
            for row in ll._reinforcement_rows(
                conn, user_s, lang_s, date.today().isoformat()
            )
        }
        rows = conn.execute(
            f'SELECT id,content_id,score,times_practiced,times_correct,times_incorrect,times_drilled,'
            f'last_practiced,last_tartarus_completed,leitner_box,leitner_last_reviewed FROM "{table}" '
            f'WHERE active=1 ORDER BY score DESC,id'
        ).fetchall()
        words = []
        for row_id,cid,score,practiced,correct,incorrect,drilled,last,last_tart,box,leitner_last in rows:
            item = material.get(cid)
            if not item:
                continue
            words.append({
                'word': item['word'],
                'score': round(float(score or 0), 1),
                'gauge': ll.score_gauge(score, ansi=False),
                'band': ll.score_band(score),
                'gauge_band': ll.score_color_band(score),
                'leitner_box': box,
                'next_maintenance': ll.maintenance_next_date(box, leitner_last),
                'maintenance_ready': row_id in ready_ids,
                'times_practiced': practiced,
                'times_correct': correct,
                'times_incorrect': incorrect,
                'times_drilled': drilled,
                'last_practiced': last,
                'last_tartarus_completed': last_tart,
                'leitner_last_reviewed': leitner_last,
                'consolidation_state': (
                    'encoding' if float(score or 0) < 9
                    else reinforcement.get(row_id, {}).get(
                        'mode', 'long_term_review'
                    )
                ),
                'consolidation_day': reinforcement.get(row_id, {}).get('day'),
            })
        return words
    finally:
        conn.close()



def load_word_list(user, lang):
    """Return editable material records without discarding any source fields."""
    path = ll.word_list_path(user, lang)
    data = ll.read_word_list(path)
    items = []
    for entry in ll.validate_word_list_items(data['items'], path):
        definition = entry.get('definition', '')
        if isinstance(definition, list):
            lines = [str(line) for line in definition]
        else:
            lines = str(definition).split('\n') if definition else []
        items.append({
            'id': entry['id'],
            'word': entry['word'],
            'definition': lines,
            'record': entry,
        })
    return {'metadata': data['metadata'], 'items': items}


def _editor_definition(item, original):
    if isinstance(item.get('definition'), list):
        return [str(line) for line in item['definition']]
    if 'definition' in item:
        return str(item['definition'])
    source = original.get('definition', '')
    if isinstance(source, list):
        lines = list(source)
        if 'def1' in item:
            if lines:
                lines[0] = str(item['def1'])
            else:
                lines.append(str(item['def1']))
        if 'def2' in item:
            while len(lines) < 2:
                lines.append('')
            lines[1] = str(item['def2'])
        return lines
    lines = str(source).split('\n') if source else []
    if 'def1' in item:
        if lines:
            lines[0] = str(item['def1'])
        else:
            lines.append(str(item['def1']))
    if 'def2' in item:
        while len(lines) < 2:
            lines.append('')
        lines[1] = str(item['def2'])
    return '\n'.join(lines)


def save_word_list(user, lang, items):
    """Losslessly save one user-owned list with stable IDs and atomic writes."""
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    target_path = ll.word_list_path_user_specific(user, lang)
    if os.path.isfile(target_path):
        source_path = target_path
        source = ll.read_word_list(source_path)
    else:
        try:
            source_path = ll.word_list_path(user, lang)
            source = ll.read_word_list(source_path)
        except FileNotFoundError:
            source_path = target_path
            source = {'metadata': ll.canonical_material_metadata(name=lang), 'items': []}
    originals = {
        entry['id']: entry
        for entry in ll.validate_word_list_items(source['items'], source_path)
    }
    saved = []
    ids = set()
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f'Invalid editor row {position}.')
        record = dict(item.get('record', {}))
        content_id = str(item.get('id', record.get('id', ''))).strip() or uuid.uuid4().hex
        if content_id in ids:
            raise ValueError(f"Each word list item needs a unique id; duplicate '{content_id}'.")
        original = originals.get(content_id, record)
        record = dict(original)
        record['id'] = content_id
        record['word'] = str(item.get('word', record.get('word', '')))
        if not record['word'].strip():
            raise ValueError(f'Invalid editor row {position}: missing word.')
        record['definition'] = _editor_definition(item, original)
        record.setdefault('word_frequency', 0)
        ids.add(content_id)
        saved.append(record)
    saved = ll.validate_word_list_items(saved, target_path)
    metadata = ll.canonical_material_metadata(source['metadata'], name=lang)
    ll.write_word_list_atomic(target_path, {'metadata': metadata, 'items': saved})
    ll.sync_word_list(user, lang)
    ll.log_event('WORD_LIST_SAVED', user=user, lang=lang, items=len(saved))
    return target_path, len(saved)

def init_word_list(user, lang, material_type='vocabulary'):
    """Create a user-owned canonical Master Schema vocabulary list."""
    user = ll.sanitize_name(user, 'user')
    lang = ll.sanitize_name(lang, 'language')
    material_type = str(material_type).strip().lower()
    if material_type != 'vocabulary':
        raise ValueError("List type must be 'vocabulary'.")
    path = ll.word_list_path_user_specific(user, lang)
    created = not os.path.exists(path)
    if created:
        ll.write_word_list_atomic(path, {
            'metadata': ll.canonical_material_metadata(
                name=lang.replace('_', ' ').title(), language='unknown',
                kind='vocabulary', level='all',
            ),
            'items': [],
        })
    conn = ll.get_connection()
    ll.ensure_user(conn, user)
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
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)
        if status >= 400:
            ll.log_event('HTTP_ERROR', path=self.path, status=status, error=data.get('error'))

    def _send_static(self, filename, content_type):
        path = os.path.join(WEB_DIR, filename)
        try:
            with open(path, 'rb') as f:
                body = f.read()
        except OSError:
            return self._send_json({'error': 'not found'}, 404)
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, body, content_type, *, cache_seconds=None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        if cache_seconds:
            # Deliberately cacheable, unlike the rest of this API: a given
            # (list, text)'s pre-generated audio is stable once generated,
            # and the same words get replayed constantly during practice.
            self.send_header('Cache-Control', f'public, max-age={cache_seconds}')
        else:
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type != 'application/json':
            raise ValueError('Content-Type must be application/json')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as error:
            raise ValueError('invalid Content-Length') from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError(f'request body exceeds {MAX_REQUEST_BYTES} bytes')
        if not length:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError('incomplete request body')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('JSON body must be an object')
        return payload

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ICON_PROBE_PATHS:
            return self._send_static('favicon.png', 'image/png')
        if not parsed.path.endswith(('.css', '.js', '.ico')):
            ll.log_event("HTTP_GET", path=parsed.path, query=parsed.query)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            return self._send_static(filename, content_type)

        if parsed.path == '/api/wordlists':
            conn = ll.get_connection()
            all_users = [row[0] for row in conn.execute("SELECT name FROM users WHERE name != 'system' ORDER BY name")]
            conn.close()
            return self._send_json({'wordlists': list_word_lists(), 'users': all_users})

        if parsed.path == '/api/report':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [None])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                response = {'reports': report_data(user, lang)}
                if lang:
                    summary = dashboard_data(user, lang)
                    if 'roadmap' in summary:
                        response['roadmap'] = summary['roadmap']
                return self._send_json(response)
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

        if parsed.path == '/api/report/today':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                return self._send_json({'entries': today_practice_overview(user)})
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)

        if parsed.path == '/api/user/progress':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            category = qs.get('category', [''])[0] or None
            level = qs.get('level', [''])[0] or None
            lang = qs.get('lang', [''])[0] or None
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            return self._send_json({'lists': user_progress_data(user, category, level, lang)})

        if parsed.path == '/api/wordlist':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                return self._send_json(load_word_list(user, lang))
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


        if parsed.path == '/api/report/trend':
            qs=urllib.parse.parse_qs(parsed.query)
            user=qs.get('user',[''])[0]; lang=qs.get('lang',[''])[0]; metric=qs.get('metric',[''])[0]
            if not user or not lang or not metric:
                return self._send_json({'error': "'user', 'lang', and 'metric' are required"},400)
            try:
                return self._send_json({'series':trend_data(user,lang,metric)})
            except ValueError as error:
                return self._send_json({'error':str(error)},400)

        if parsed.path == '/api/export':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            return self._send_json(ll.export_user_data(user))

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

        if parsed.path == '/api/audio':
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get('user', [''])[0]
            lang = qs.get('lang', [''])[0]
            text = qs.get('text', [''])[0]
            if not user or not lang or not text:
                return self._send_json({'error': "'user', 'lang', and 'text' are required"}, 400)
            result = ll.lookup_bundled_audio(user, lang, text)
            if result is None:
                return self._send_json({'error': 'not found'}, 404)
            audio_bytes, content_type = result
            return self._send_binary(audio_bytes, content_type, cache_seconds=604800)

        if parsed.path == '/api/consolidation/progress':
            qs=urllib.parse.parse_qs(parsed.query); user=qs.get('user',[''])[0]; lang=qs.get('lang',[''])[0]
            if not user or not lang: return self._send_json({'error': "'user' and 'lang' are required"},400)
            try:
                user=ll.sanitize_name(user,'user'); lang=ll.sanitize_name(lang,'language'); conn=ll.get_connection()
                try:
                    table=ll.words_table_name(user,lang)
                    distribution={str(i):0 for i in range(1,11)}
                    if ll.table_exists(conn,table):
                        for box,count in conn.execute(f'SELECT leitner_box,COUNT(*) FROM "{table}" WHERE active=1 AND leitner_box IS NOT NULL GROUP BY leitner_box'): distribution[str(box)]=count
                    state=ll.consolidation_state_breakdown(user,lang,conn=conn)
                    consolidation_roadmap=_consolidation_roadmap_payload(state)
                    progress_payload={**consolidation_roadmap,'max_day':ll.CONSOLIDATION_MAX_DAY}
                    payload={'progress':progress_payload,'roadmap':{'consolidation':consolidation_roadmap,'leitner_distribution':distribution,'maintenance_ready':len(ll.maintenance_ready_words(user,lang))}}
                    return self._send_json(payload)
                finally: conn.close()
            except (ValueError,FileNotFoundError) as e: return self._send_json({'error':str(e)},400)

        return self._send_json({'error': 'not found'}, 404)


    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        ll.log_event("HTTP_POST", path=parsed.path)
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError:
            return self._send_json({'error': 'invalid JSON body'}, 400)
        except ValueError as error:
            return self._send_json({'error': str(error)}, 400)

        if parsed.path == '/api/client-log':
            message = str(payload.get('message', '')).strip()[:2000]
            if not message:
                return self._send_json({'error': "'message' is required"}, 400)
            level = str(payload.get('level', 'error')).strip().lower()
            if level not in ('error', 'warn', 'info'):
                level = 'error'
            ll.log_event(
                f'CLIENT_{level.upper()}', message=message,
                url=str(payload.get('url', ''))[:300],
                user=str(payload.get('user', ''))[:100],
                stack=str(payload.get('stack', ''))[:2000],
            )
            return self._send_json({'status': 'ok'})

        if parsed.path == '/api/tts':
            text = str(payload.get('text', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            # Deterministic non-macOS test double: emulate blocking `say`
            # without changing production behavior.
            try:
                test_delay_ms = max(0, int(os.environ.get('TARTARUS_TTS_TEST_DELAY_MS', '0')))
            except (TypeError, ValueError):
                test_delay_ms = 0
            if test_delay_ms:
                time.sleep(test_delay_ms / 1000.0)
                return self._send_json({'supported': True, 'spoken': bool(text), 'simulated': True})
            if not ll.tts_available():
                return self._send_json({'supported': False, 'spoken': False, 'error': 'Speech is supported only on macOS with say installed.'}, 501)
            try:
                spoken = ll.speak(text, lang or None)
            except ValueError as error:
                return self._send_json({'error': str(error)}, 400)
            return self._send_json({'supported': True, 'spoken': spoken})


        if parsed.path == '/api/import':
            user = str(payload.get('user', '')).strip()
            data = payload.get('data', {})
            if not user or not data:
                return self._send_json({'error': "'user' and 'data' are required"}, 400)
            try:
                ll.import_user_data(user, data)
            except ValueError as error:
                return self._send_json({'error': str(error)}, 400)
            return self._send_json({'status': 'ok'})

        if parsed.path == '/api/user/create':
            user = str(payload.get('user', '')).strip()
            if not user:
                return self._send_json({'error': "user required"}, 400)
            conn = ll.get_connection()
            ll.ensure_user(conn, user)
            ll.ensure_sessions_table(conn, user)
            conn.commit()
            conn.close()
            return self._send_json({'status': 'ok'})

        if parsed.path == '/api/wordlist/restart':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                ll.reset_word_list_progress(user, lang)
            except ValueError as error:
                return self._send_json({'error': str(error)}, 400)
            return self._send_json({'status': 'ok'})

        if parsed.path == '/api/user/shift-dates':
            user = str(payload.get('user', '')).strip()
            if not user:
                return self._send_json({'error': "'user' is required"}, 400)
            try:
                result = ll.shift_user_dates_forward(user)
            except ValueError as error:
                return self._send_json({'error': str(error)}, 400)
            return self._send_json({
                'status': 'ok',
                'shifted': result['shifted'],
                'gap_days': result['gap_days'],
                'shift_days': result['shift_days'],
                'reason': result['reason'],
                'last_practiced': result['last_practiced'],
                'rows_updated': sum(result['tables'].values()),
            })

        if parsed.path == '/api/wordlist/custom':
            user = str(payload.get('user', '')).strip()
            list_name = str(payload.get('list_name', '')).strip()
            items = payload.get('items', [])
            if not user or not list_name or not items:
                return self._send_json({'error': "user, list_name, and items required"}, 400)
            path = ll.save_custom_list(user, list_name, items)
            return self._send_json({'status': 'ok', 'path': path})

        if parsed.path == '/api/init':
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            try:
                created, path = init_word_list(user, lang, payload.get('type', 'vocabulary'))
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'created': created, 'path': path})

        if parsed.path == '/api/practice/start':
            allowed = {'user', 'lang', 'audio_lang', 'track'}
            unknown = set(payload) - allowed
            if unknown:
                return self._send_json({'error': f"unsupported practice option(s): {', '.join(sorted(unknown))}"}, 400)
            user = str(payload.get('user', '')).strip()
            lang = str(payload.get('lang', '')).strip()
            audio_lang = str(payload.get('audio_lang', '')).strip() or None
            track = str(payload.get('track', '')).strip() or None
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            if track is not None and track not in ll.PRACTICE_BUCKET_TRACKS:
                return self._send_json({'error': f"unknown practice track: {track}"}, 400)

            try:
                if track is not None:
                    # === SUPPLEMENTARY PRACTICE TRACK: never touches score/Leitner/consolidation_step ===
                    session_id, session, consolidation_meta = bucket_start_session(
                        user, lang, track, audio_lang=audio_lang,
                    )
                else:
                    # === CONSOLIDATION TRACK: backend decides everything ===
                    session_id, session, consolidation_meta = consolidation_start_session(
                        user, lang, audio_lang=audio_lang,
                    )
            except (ValueError, FileNotFoundError) as e:
                return self._send_json({'error': str(e)}, 400)

            question = next_question(session)
            return self._send_json({
                'session_id': session_id,
                'lang': session['lang'],
                'audio_lang': session['voice_lang'],
                'consolidation': consolidation_meta,
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
            items = payload.get('items', payload.get('words', []))
            if not user or not lang:
                return self._send_json({'error': "'user' and 'lang' are required"}, 400)
            try:
                path, count = save_word_list(user, lang, items)
            except ValueError as e:
                return self._send_json({'error': str(e)}, 400)
            return self._send_json({'saved': True, 'path': path, 'count': count})

        if parsed.path == '/api/practice/cancel':
            cleanup_sessions()
            session_id = str(payload.get('session_id', '')).strip()
            with SESSIONS_LOCK:
                session = SESSIONS.get(session_id)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            with session['lock']:
                if (session.get('current') or {}).get('drill') is not None:
                    return self._send_json({'error': 'Complete the mandatory drill before ending the session.'}, 409)
                with SESSIONS_LOCK:
                    SESSIONS.pop(session_id, None)
                summary = finalize_session(session, ended_early=True)
            return self._send_json({'cancelled': True, 'session': summary})

        if parsed.path in {'/api/practice/answer', '/api/practice/timeout'}:
            cleanup_sessions()
            session_id = payload.get('session_id')
            with SESSIONS_LOCK:
                session = SESSIONS.get(session_id)
            if session is None:
                return self._send_json({'error': 'unknown or expired session'}, 404)
            question_id = str(payload.get('question_id', '')).strip()
            sequence = payload.get('sequence')
            attempt_id = str(payload.get('attempt_id', '')).strip()
            with session['lock']:
                session['last_activity'] = time.time()
                session['expires_at'] = session['last_activity'] + SESSION_TTL_SECONDS
                cached = session['answer_results'].get(attempt_id) if attempt_id else None
                if cached is not None:
                    return self._send_json(cached)
                current = session.get('current') or {}
                if not question_id or question_id != current.get('question_id'):
                    return self._send_json({'error': 'stale or missing question id'}, 409)
                if sequence != current.get('sequence'):
                    return self._send_json({'error': 'stale or missing question sequence'}, 409)
                if not attempt_id:
                    return self._send_json({'error': 'missing answer idempotency key'}, 400)
                try:
                    result = process_answer(
                        session,
                        payload.get('answer', ''),
                        timed_out=(parsed.path == '/api/practice/timeout'),
                    )
                except Exception as e:
                    import traceback; traceback.print_exc()
                    with SESSIONS_LOCK:
                        SESSIONS.pop(session_id, None)
                    if session.get('practiced', 0) > 0:
                        finalize_session(session, ended_early=True)
                    return self._send_json({'error': f'Internal error processing answer: {str(e)}'}, 500)
                session['answer_results'][attempt_id] = result
                if result.get('done'):
                    with SESSIONS_LOCK:
                        SESSIONS.pop(session_id, None)
                return self._send_json(result)

        return self._send_json({'error': 'not found'}, 404)


class TartarusHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import traceback
        exc_type, exc_value, _ = sys.exc_info()
        if exc_type in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The client (browser tab closed, refreshed, or navigated away)
            # disconnected mid-response. Ordinary for a local dev server, not a
            # bug -- log it plainly and skip the default handler, which would
            # otherwise print a full traceback to stderr for a non-event.
            ll.log_event("CLIENT_DISCONNECTED", client=str(client_address), error=str(exc_value))
            return
        ll.log_event("SERVER_CRASH", client=str(client_address), error=traceback.format_exc())
        super().handle_error(request, client_address)

def _loopback_host(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower()=='localhost'


def main():
    ll.configure_logging()
    if not _loopback_host(HOST):
        raise ValueError('Tartarus release mode is localhost-only; use 127.0.0.1, ::1, or localhost.')
    ll.initialize_database()
    try: httpd=TartarusHTTPServer((HOST,PORT),Handler)
    except OSError as e:
        if e.errno==errno.EADDRINUSE:
            print(f'Error: port {PORT} is already in use.'); sys.exit(1)
        raise
    print(f'Tartarus web server: http://{HOST}:{PORT}/')
    try: httpd.serve_forever()
    except KeyboardInterrupt: print('\nShutting down...')
    finally: httpd.server_close()



if __name__ == '__main__':
    main()
