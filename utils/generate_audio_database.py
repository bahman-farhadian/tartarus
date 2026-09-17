#!/usr/bin/env python3
"""Idempotent generator: pre-render `say` pronunciation audio for every
bundled item into small, per-source-file SQLite databases.

Meant to be re-run whenever the dataset changes, not just once. Each source
JSON file gets one independent output database (mirroring its relative path
under data/audio/, e.g. data/word_lists/german/vocabulary/a1/foo.json ->
data/audio/german/vocabulary/a1/foo.part1.db), so nothing ever needs to be
committed as one multi-hundred-MB blob. A handful of the largest files
(~5000 items) would still exceed a single 100MB file at 96 kbps, so any file
that would cross MAX_PART_BYTES automatically continues into foo.part2.db,
foo.part3.db, etc. Small files just get a single .part1.db.

Change detection is per source file, not global, so editing one JSON file
never triggers work on any other file:

1. Does this file already have an audio db at all? If not, generate it from
   scratch.
2. If it does, compare a content hash of the file's current word list
   (order-independent set of unique `word` strings + the speech rate)
   against the hash stored in that db from the last run. Unchanged -> skip
   entirely, no per-item work at all.
3. Changed -> reconcile, not regenerate: words newly present get generated,
   words no longer present get their stored audio deleted (VACUUM'd back),
   words already present and still present are left untouched. A file with
   4000 unchanged items and 1 new one only ever generates that 1 item.

Strictly sequential, as required: for each item, `say` runs to completion,
is converted to AAC/M4A at 96 kbps via `afconvert`, and is committed before
the next item starts. No batching, no parallel `say` processes.

German files are generated before English files.

Usage:
    python3 utils/generate_audio_database.py
    python3 utils/generate_audio_database.py --language german
    python3 utils/generate_audio_database.py --force            # ignore hashes, reconcile every file
    python3 utils/generate_audio_database.py --limit-files 2 --limit-items 5   # smoke test
"""
import argparse
import glob
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tartarus as ll  # noqa: E402

# Path/stem conventions live once, in tartarus.py, and are reused here so the
# generator (write path) and the runtime lookup (read path) can never drift
# apart: WORD_LISTS_DIR, AUDIO_DIR, ll.audio_relative_stem, ll.audio_part_path,
# ll.bundled_audio_db_paths.
WORD_LISTS_DIR = ll.WORD_LISTS_DIR
AUDIO_DIR = ll.AUDIO_DIR

RATE = ll.SPEECH_RATE  # same fixed rate the live say() fallback uses, so generated and live audio match
SAMPLE_RATE = 44100    # afconvert needs an explicit sample rate for -b to take effect
BITRATE = 96000        # 96 kbps
CONTENT_TYPE = 'audio/mp4'
MAX_PART_BYTES = 90 * 1024 * 1024  # 90MB safety margin under GitHub's 100MB hard limit


def source_files(language):
    return sorted(glob.glob(os.path.join(WORD_LISTS_DIR, language, '*', '*', '*.json')))


def ordered_unique_texts(json_path):
    """First-seen-wins `word` strings for one file, in item order."""
    with open(json_path, encoding='utf-8') as handle:
        data = json.load(handle)
    seen = set()
    ordered = []
    for item in data.get('items', []):
        text = str(item.get('word', '')).strip()
        if text and text not in seen:
            seen.add(text)
            ordered.append(text)
    return ordered


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS audio (
        id INTEGER PRIMARY KEY,
        source_file TEXT NOT NULL,
        language TEXT NOT NULL,
        text TEXT NOT NULL,
        voice TEXT,
        rate INTEGER NOT NULL,
        content_type TEXT NOT NULL,
        audio BLOB NOT NULL,
        byte_size INTEGER NOT NULL,
        generated_at TEXT NOT NULL,
        UNIQUE(text)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    conn.commit()


def source_hash(items):
    """Order-independent fingerprint of what audio this file currently needs.
    Only the unique word set and the speech rate matter -- definition/example
    text, metadata, and item order never affect what needs to be spoken."""
    digest = hashlib.sha256()
    digest.update(f'rate={RATE}\n'.encode('utf-8'))
    for text in sorted(set(items)):
        digest.update(text.encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def existing_parts(stem):
    """[(part_number, path), ...] of part files already on disk for one stem,
    in order. Parts are always written sequentially with no gaps, so this is
    exactly ll.bundled_audio_db_paths() with its implicit numbering made
    explicit for callers that need to address a specific part."""
    return list(enumerate(ll.bundled_audio_db_paths(stem), start=1))


def stored_hash(stem):
    """The fingerprint recorded after the last successful run for this file,
    stored in its part1 db, or None if there's no db yet / no recorded hash."""
    parts = existing_parts(stem)
    if not parts or parts[0][0] != 1:
        return None
    conn = sqlite3.connect(parts[0][1])
    try:
        ensure_schema(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='source_hash'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_stored_hash(stem, digest):
    parts = existing_parts(stem)
    if not parts:
        return
    part1_path = dict(parts).get(1)
    if not part1_path:
        return
    conn = sqlite3.connect(part1_path)
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('source_hash',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (digest,),
        )
        conn.commit()
    finally:
        conn.close()


def existing_texts_by_part(stem):
    """{text: part_number} across every existing part file for one stem."""
    located = {}
    for part_number, path in existing_parts(stem):
        conn = sqlite3.connect(path)
        try:
            located.update(
                (row[0], part_number) for row in conn.execute('SELECT text FROM audio')
            )
        finally:
            conn.close()
    return located


def remove_stale_texts(stem, stale, located):
    """Delete audio no longer referenced by the source JSON, then reclaim
    the space -- these are BLOB-heavy tables where deleted rows otherwise
    leave the file exactly as large as before."""
    by_part = {}
    for text in stale:
        by_part.setdefault(located[text], []).append(text)
    for part_number, texts in by_part.items():
        path = ll.audio_part_path(stem, part_number)
        conn = sqlite3.connect(path)
        try:
            conn.executemany('DELETE FROM audio WHERE text=?', ((t,) for t in texts))
            conn.commit()
            conn.execute('VACUUM')
        finally:
            conn.close()


def open_part_for_writing(stem):
    """(conn, part_number, current_bytes) for the part file to append into --
    the last existing part if it has room, otherwise a fresh one."""
    parts = existing_parts(stem)
    if parts:
        part_number, path = parts[-1]
        conn = sqlite3.connect(path)
        ensure_schema(conn)
        current_bytes = conn.execute('SELECT COALESCE(SUM(byte_size),0) FROM audio').fetchone()[0]
        if current_bytes < MAX_PART_BYTES:
            return conn, part_number, current_bytes
        conn.close()
        part_number += 1
    else:
        part_number = 1
    path = ll.audio_part_path(stem, part_number)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    ensure_schema(conn)
    return conn, part_number, 0


def generate_one(text, voice, work_dir):
    """say -> AIFF -> afconvert -> AAC/M4A bytes. Fully sequential, blocking."""
    aiff_path = os.path.join(work_dir, 'item.aiff')
    m4a_path = os.path.join(work_dir, 'item.m4a')
    for path in (aiff_path, m4a_path):
        if os.path.exists(path):
            os.remove(path)

    say_cmd = ['say', '-r', str(RATE)]
    if voice:
        say_cmd += ['-v', voice]
    say_cmd += ['-o', aiff_path, text]
    subprocess.run(say_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    convert_cmd = [
        'afconvert', '-f', 'm4af', '-d', f'aac@{SAMPLE_RATE}',
        '-b', str(BITRATE), '-s', '0', aiff_path, m4a_path,
    ]
    subprocess.run(convert_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    with open(m4a_path, 'rb') as handle:
        return handle.read()


def process_file(json_path, language, voice, work_dir, limit_items=None, force=False):
    stem = ll.audio_relative_stem(json_path)
    items = ordered_unique_texts(json_path)
    if not items:
        return

    current_hash = source_hash(items)
    has_db = bool(existing_parts(stem))
    if has_db and not force and stored_hash(stem) == current_hash:
        print(f'-- {stem}: unchanged since last run ({len(items)} items), skipping')
        return

    located = existing_texts_by_part(stem)
    existing = set(located)
    stale = existing - set(items)
    pending = [text for text in items if text not in existing]
    truncated = limit_items is not None and len(pending) > limit_items
    if limit_items is not None:
        pending = pending[:limit_items]

    if stale:
        remove_stale_texts(stem, stale, located)
        print(f'-- {stem}: removed {len(stale)} item(s) no longer in the source JSON')

    if not pending:
        if stale:
            set_stored_hash(stem, current_hash)
        else:
            print(f'-- {stem}: already complete ({len(items)} items)')
        return

    print(f'-- {stem}: {len(items)} items, {len(pending)} pending')
    conn, part_number, current_bytes = open_part_for_writing(stem)
    start = time.time()
    try:
        for index, text in enumerate(pending, start=1):
            try:
                audio = generate_one(text, voice, work_dir)
            except subprocess.CalledProcessError as error:
                stderr = error.stderr.decode('utf-8', 'replace') if error.stderr else ''
                print(f'   ! FAILED on {text!r}: {stderr.strip() or error}', file=sys.stderr)
                continue

            if current_bytes and current_bytes + len(audio) > MAX_PART_BYTES:
                conn.close()
                part_number += 1
                path = ll.audio_part_path(stem, part_number)
                conn = sqlite3.connect(path)
                ensure_schema(conn)
                current_bytes = 0
                print(f'   -> rolling to part {part_number}')

            conn.execute(
                'INSERT OR IGNORE INTO audio'
                '(source_file,language,text,voice,rate,content_type,audio,byte_size,generated_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (
                    stem, language, text, voice, RATE, CONTENT_TYPE, audio, len(audio),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            current_bytes += len(audio)

            elapsed = time.time() - start
            remaining = (elapsed / index) * (len(pending) - index)
            print(
                f'   [{index}/{len(pending)}] part{part_number} '
                f'({elapsed:.0f}s elapsed, ~{remaining / 60:.1f}m left) '
                f'{text!r} -> {len(audio)} bytes'
            )
    finally:
        conn.close()

    if truncated:
        print(f'-- {stem}: --limit-items truncated this run; not marking up to date')
    else:
        set_stored_hash(stem, current_hash)


def run(languages, limit_files=None, limit_items=None, force=False):
    if sys.platform != 'darwin':
        raise SystemExit('Audio generation requires macOS (say + afconvert).')
    if not ll.tts_available():
        raise SystemExit("macOS 'say' is not available on this machine.")

    with tempfile.TemporaryDirectory(prefix='tartarus-audio-gen-') as work_dir:
        for language in languages:
            voice = ll.voice_for_language(language)
            files = source_files(language)
            if limit_files is not None:
                files = files[:limit_files]
            print(f'=== {language} === voice={voice or "(system default)"} rate={RATE} files={len(files)}')
            for json_path in files:
                process_file(json_path, language, voice, work_dir, limit_items=limit_items, force=force)
    print('Done.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--language', choices=('german', 'english'), default=None,
        help='Generate only this language (default: german then english).',
    )
    parser.add_argument('--limit-files', type=int, default=None, help='Only process this many source files per language (smoke test).')
    parser.add_argument('--limit-items', type=int, default=None, help='Only process this many pending items per file (smoke test).')
    parser.add_argument('--force', action='store_true', help='Reconcile every file even if its content hash matches the last run.')
    args = parser.parse_args()
    languages = [args.language] if args.language else ['german', 'english']
    run(languages, limit_files=args.limit_files, limit_items=args.limit_items, force=args.force)


if __name__ == '__main__':
    main()
