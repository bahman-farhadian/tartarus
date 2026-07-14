# Tartarus - Agent Instructions

## Project Overview
A vocabulary practice tool with CLI and self-hosted web UI, backed by SQLite with spaced-repetition scoring. Language-agnostic, multi-user. No external dependencies (stdlib only). Audio on macOS via `say`.

## Commands (via Makefile)
```bash
make help                      # Show commands
make web                       # Start web UI at http://127.0.0.1:9999
make practice user=X list=Y    # CLI practice session
make report user=X [list=Y]    # Progress report
make init user=X list=Y        # Create empty word list
# Pass CLI flags: make practice user=X list=Y opts="--no-audio --drill-mode"
```

## Key Files
- `tartarus.py` - CLI entry point (all logic)
- `tartarus_web.py` - Web server (JSON API + static files)
- `tartarus.sh` / `tartarus_web.sh` - Python wrappers (use these, not `python3` directly)
- `Makefile` - Canonical entry points
- `data/word_lists/<user>_<lang>.json` - User word lists (JSON array of `{word, definition?}`)
- `data/tartarus.db` - SQLite database (auto-created)
- `web/` - Static frontend (HTML/CSS/JS, Catppuccin Mocha theme)
- `utils/generate_tartarus_json.py` - Generate word lists from source decks

## Database Schema (per user)
- `words_<user>_<lang>` - word tables: `text`, `definition`, `score` (1.0-9.0), `active`, `times_practiced`, `times_correct`, `times_incorrect`, `times_drilled`, `times_flagged`, `times_mastered`, `leitner_box` (1-5), `last_practiced`, `last_decay_at`, `last_known_review_at`
- `sessions_<user>` - session logs

## Score Bands & Question Types
| Score | Band | Type | Correct | Incorrect |
|-------|------|------|---------|-----------|
| 1-3   | 1    | Learning (word+def shown) | +1 | -2 |
| 4-6   | 2    | Audio only | +2 | -2 |
| 7-9   | 3    | Production (def+audio) | +3 (cap 9) | -1 |
- Score floored at 1.0. Mastered (9.0) → Leitner box system (1/2/4/9/14 day intervals).
- Decay: idle words lose 1.0/day (floored at 1.0), applied on each `practice`/`report`.

## Word List Format (`data/word_lists/<user>_<lang>.json`)
```json
[{ "word": "Haus", "definition": ["house", "ein Gebäude"] }, { "word": "laufen" }]
```
- `word`: required, comma-separated forms accepted (`"das Haus, die Häuser"`)
- `definition`: optional, string or string[] (multi-line), any language

## Special Session Commands
| Input | Effect |
|-------|--------|
| `!!` / `Ctrl+C` | End session, save progress |
| `?` | Repeat/reveal word or replay audio |
| `+` | Replay audio |
| `!` | Flag word (score→1.0) |
| `@` | Master word (score→9.0) |
| `$` | Start 9-rep drill (score→5.0) |

## Modes
- **Normal** (`practice`): 16 unique words, mixed bands, Leitner scheduling
- **Drill all** (`--drill`): Every word gets 9-rep drill
- **Drill mode** (`--drill-mode`): Top 10 most-missed words, 9-rep drill, clears 1 incorrect mark per completion
- **Sentence mode** (`lang` contains "sentences"): Native sentence shown, progressive masking, score 0→9, +1/correct, no drill

## Audio (macOS only)
- Enabled by default, disable with `--no-audio`
- Voice auto-selected by `--lang`/`--audio-lang` (e.g., German → Anna voice)
- Use `--audio-lang german` for sub-lists like `german_b1`
- WPM: `--wpm` (default 128)

## Web UI
```bash
make web  # Runs on http://127.0.0.1:9999 (localhost only)
```
- Practice, Report (with Leitner box view), Word Lists (CRUD in browser), About
- Audio via server-side `say` (same voices as CLI)

## Generating Word Lists
Source decks in `data/word_lists/` (from `vbvss199/Language-Learning-decks`):
```bash
# Vocab mode (per CEFR level)
python3 utils/generate_tartarus_json.py --lang german --user bahman
python3 utils/generate_tartarus_json.py --lang kanji --user tartarus

# Sentence mode
python3 utils/generate_tartarus_json.py --lang german --user bahman --sentences

# Single level / flashcard-only
python3 utils/generate_tartarus_json.py --lang german --user bahman --cefr B1
python3 utils/generate_tartarus_json.py --lang kanji --user tartarus --flashcard-only
```
Output: `data/word_lists/<user>_<lang>_<level>.json` or `..._sentences_<level>.json`

## Video Generator (optional)
```bash
chmod +x make_tartarus_video.sh
./make_tartarus_video.sh --user bahman --lang german
# Requires ffmpeg with drawtext (Homebrew: brew install ffmpeg-full)
```

## Key Conventions
- Always run via `tartarus.sh` / `tartarus_web.sh` / `make`
- Python 3 stdlib only, no venv/pip
- Word list filename = table name suffix: `<user>_<lang>`
- `lang` containing "sentences" → sentence mode (auto-detected)
- German nouns should include article (`der`/`die`/`das`) for gender coloring
- Database auto-migrates on sync; no migration scripts needed

## Testing / Verification
No formal test suite. Verify manually:
```bash
make init user=test list=demo
echo '[{"word": "Haus", "definition": "house"}]' > data/word_lists/test_demo.json
make practice user=test list=demo
make report user=test list=demo
make web  # open http://127.0.0.1:9999
```