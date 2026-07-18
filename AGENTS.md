# AGENTS.md — Tartarus

## Project Overview
Vocabulary practice tool with CLI and localhost web UI. Single SQLite database, spaced repetition (Leitner 5-box + score bands), macOS `say` for audio. **Zero external dependencies** — Python stdlib only.

## Key Commands (via Makefile)
```
make help                # Show all commands
make web                 # Start web UI at http://127.0.0.1:9999
make practice user=X list=Y [opts="--no-audio"]  # CLI practice session
make report user=X [list=Y]  # Progress report (omit list for all languages)
make init user=X list=Y      # Create empty word list JSON + DB tables
make video opts="--user X --lang Y"  # Generate vocab video (needs ffmpeg-full)
```

CLI flags (pass via `opts="..."`):
- `--no-audio` — disable macOS `say` (default: on)
- `--audio-lang <lang>` — force TTS voice (e.g., `german` for `german_home` lists)
- `--drill` — mistake drill (top 10 most-missed, 9× correct-in-a-row)
- `--drill-mode` — same as `--drill`
- `--instant-drill` — instant 9× drill on every wrong answer
- `--drill-all` — full 9× drill for every word in session
- `--known-drill` — review mastered words oldest-review-first
- `--fast` — fast review of mastered words only
- `--wpm <n>` — speech rate (default 128)

## Architecture
```
utils/tartarus.py       # CLI entry point, all core logic (DB, scoring, practice flow)
utils/tartarus_web.py   # HTTP server (stdlib http.server), JSON API + static files
web/index.html, style.css, app.js  # Frontend (Catppuccin Mocha dark theme)
utils/make_tartarus_video.py  # Standalone video generator (needs ffmpeg-full)
Makefile                # Primary entry points (see above)
data/tartarus.db        # SQLite DB (auto-created, gitignored)
data/word_lists/        # JSON word lists (user-specific + shared categorized)
```

### Database
- One file: `data/tartarus.db`
- Per-user tables: `words_<user>_<lang>`, `sessions_<user>`
- Word columns: `text`, `definition`, `score` (1.0–9.0), `leitner_box` (1–5), `last_practiced`, `last_decay_at`, history counters (`times_practiced`, `times_correct`, `times_incorrect`, `times_drilled`, `times_mastered`, `times_flagged`)
- Sentence lists (lang contains "sentences"): integer score 0–9, +1 per correct, drill disabled
- Auto-decay: inactive words lose 1.0/day (floor 1.0) on `practice`/`report --lang` run
- Leitner: mastered (9.0) words enter box 1 → 2 → 3 → 4 → 5 (intervals 1/2/4/9/14 days); incorrect resets to box 1

### Word Lists
Location: `data/word_lists/<user>_<lang>.json` (user-created) or `data/word_lists/<lang>/vocabulary|sentences/<level>/<name>.json` (shared).
```json
[
  { "word": "Haus", "definition": ["house, building", "ein Gebäude zum Wohnen"] },
  { "word": "laufen", "definition": "to run, to walk" },
  { "word": "Apfel" }
]
```
- `word`: required; comma-separated forms accepted (`"das Haus, die Häuser"`)
- `definition`: optional; string or array; shown line-by-line

### In-Session Commands (CLI & Web)
| Key | Action |
|-----|--------|
| `!!` / Ctrl+C | End session early, save progress |
| `?` | Reveal word / replay audio |
| `+` | Replay audio |
| `!` | Flag word (score → 1.0) |
| `@` | Master word (score → 9.0) |
| `$` | Drill word (9× correct-in-a-row, score → 5.0) |

### Audio (macOS only)
- Enabled by default via `say`; `--no-audio` to disable
- Voice selection: English → system default; German → prefers `Anna (Premium)` > `Anna (Enhanced)` > `Anna`
- `--audio-lang german` forces German voice for sub-lists like `german_home`

### Video Generation (`make video`)
- Requires `ffmpeg-full` (Homebrew: `brew uninstall ffmpeg && brew install ffmpeg-full`) for `drawtext` filter
- Uses same `say` voice logic as CLI
- Output: `videos/<user>_<lang>.mp4`

## Conventions & Gotchas
- **User/lang names**: lowercase, digits, underscore only (`sanitize_name` validates)
- **Score bands**: 1–3 = Learning (show word+def), 4–6 = Audio (listen only), 7–9 = Production (def+audio → type from memory)
- **Sentence mode**: detected by `"sentences"` in lang name; different scoring (0–9 int), no drill
- **Web UI**: bound to `127.0.0.1:9999` only; sessions in memory (lost on restart, progress auto-saved to DB per answer)
- **No tests, no linter, no type checker** — stdlib only, no CI config
- **Database migrations**: additive only (ALTER TABLE in `ensure_word_table`), run on `practice`/`report`/`init`
- **German gender colors**: `der`=blue, `die`=red, `das`=green (applied if word starts with article)

## File Ownership
- `utils/tartarus.py` — all core logic (single file, ~2500 lines)
- `utils/tartarus_web.py` — web server + API (imports `tartarus` as `ll`)
- `web/app.js` — all frontend logic (vanilla JS, no build step)
- `utils/make_tartarus_video.py` — standalone video script
- `Makefile` — command aliases only

## Working in This Repo
- Run via `make <target>` — direct `python3 utils/tartarus.py` works but requires full flags
- Database is local-only; no migration scripts needed (auto-migrates on use)
- Add words by editing `data/word_lists/<user>_<lang>.json` then run `practice` or `report --lang` to sync
- No virtualenv, no `pip install`, no `requirements.txt`