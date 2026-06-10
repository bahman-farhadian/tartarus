# LexiLoop

An interactive command-line tool for vocabulary practice, with a SQLite-backed
spaced-repetition scoring system. LexiLoop is **language-agnostic and
multi-user**: any user can maintain any number of word lists (one per
language or topic), each entry being a word plus an optional definition (or
multiple definitions, in any language). For example, an English word can have
both an English and a German definition, and vice versa — handy if you're
practicing a new language while reviewing in your own.

## How it works

- All data lives in a single local SQLite database (`data/lexiloop.db`).
- Each **user** has their own tables, and each **word list** (one per
  `--lang`) is its own table: `words_<user>_<lang>`.
- Every word has a **score** from `1.0` (struggling) to `9.0` (mastered),
  plus history counters (`times_practiced`, `times_correct`,
  `times_incorrect`, `times_drilled`, `times_flagged`, `times_mastered`).
- There's a single `practice` command. Each word's *current score* decides
  what kind of question it gets, Memrise-style — so a session over a mix of
  new and practiced words naturally mixes all three question types:

| Score | Gauge | Question type | On correct | On incorrect |
|---|---|---|---|---|
| 1-3 | `○○○` | **Learning** — word + definition(s) shown, type the word | `+1` | `-2` |
| 4-6 | `●○○` | **Audio** — listen only, type the word you hear | `+2` | `-2` |
| 7-9 | `●●○`/`●●●` | **Meaning** — word shown, pick its meaning (a-d) | `+3` (capped at 9.0) | `-2` |

Scores are floored at `1.0`. A word with no definition always uses the
flash-and-hide spelling test for "Learning", and the listening test for
"Meaning" (since there's no meaning to quiz), but still earns/loses the
points for whichever band it's in.

```mermaid
flowchart LR
    L["Learning\nscore 1-3"]
    A["Audio\nscore 4-6"]
    M["Meaning\nscore 7-9"]

    L -- "score >= 4" --> A
    A -- "score >= 7" --> M
    A -- "score <= 3" --> L
    M -- "score <= 6" --> A
```

A correct answer adds points (+1 in Learning, +2 in Audio, +3 in Meaning,
capped at 9); an incorrect answer subtracts 2 (floored at 1) — see the table
above. Either can move a word into a neighboring band, as shown. Manual
overrides jump straight to a band regardless of score: `@` master -> 9.0
(Meaning), `$` drill -> 5.0 (Audio), `!` flag -> 1.0 (Learning).

- Every word left untouched for **a week or more automatically loses 1.0
  point per idle week** (floored at `1.0`), pulling neglected words back
  into easier question types over time — this happens automatically on every
  `practice`/`report --lang` run, no separate command needed.
- Every session is logged (date, duration, words practiced, correct/incorrect,
  drilled count) so you can review your history with `report`.

## Setup

LexiLoop is a single Python file (`lexiloop.py`), run through the
`lexiloop.sh` wrapper — don't call the `.py` file directly.

```bash
chmod +x lexiloop.sh   # one-time, if not already executable
```

### Create a word list for a user/language

```bash
./lexiloop.sh init --user bahman --lang german
```

This creates `data/word_lists/bahman_german.json` (an empty array) and the
corresponding tables in `data/lexiloop.db`. Edit the JSON file to add words,
then practice.

## Word list format

Each word list is a JSON array of `{word, definition}` objects, stored at
`data/word_lists/<user>_<lang>.json`:

```json
[
  { "word": "Haus", "definition": ["house, building", "ein Gebäude zum Wohnen"] },
  { "word": "laufen", "definition": "to run, to walk" },
  { "word": "Apfel" }
]
```

- `word` — required, the term to practice. You can give **multiple accepted
  forms** by separating them with commas, e.g. `"das Haus, die Häuser"`
  (singular + plural). All forms are shown/spoken together, and an answer is
  marked correct if it matches *any single form*, **or** if you type out all
  the forms together exactly as shown (in the same order) — spacing around
  the commas doesn't matter (`"a, b"`, `"a,b"` and `"a , b"` are all
  equivalent), and matching is case-insensitive.
- `definition` — optional. Can be a single string, a list of strings (each
  shown on its own line), or omitted entirely for plain spelling practice.
  Definitions can be in any language(s) you like — there's no fixed pairing.

> **Note:** the "Learning" and "Meaning" question types only do anything
> useful for words that *have* a non-empty `definition`. A word with no
> definition always falls back to the plain flash-and-hide spelling test for
> "Learning", and to the listening test for "Meaning" (no meaning to quiz).
> For the best experience, give every word at least one definition.

Sample lists are included for user `bahman`:

- `data/word_lists/bahman_english.json` — 20 A1 English words with
  English-only definitions, for an English speaker reviewing their own
  language. Only irregular plurals (e.g. `"child, children"`) are given as
  multi-form entries; regular `-s`/`-es` plurals are omitted.
- `data/word_lists/bahman_german.json` — 20 A1 German words (with articles
  and plural forms) and English definitions, for an English speaker learning
  German.

## Renewing word lists

Every time you run `practice` or `report --lang <lang>`, LexiLoop "renews"
that list from its JSON file:

- New entries are added to the table (score `1.0`, fresh history).
- Existing entries have their definitions refreshed.
- Entries removed from the JSON file are **deactivated** (excluded from
  future practice) but their score and history counters are kept — if you
  add the word back later, its history picks up where it left off.

```mermaid
flowchart TD
    A["init --user --lang"] --> B["edit data/word_lists/&lt;user&gt;_&lt;lang&gt;.json"]
    B --> C["practice / report --lang"]
    C --> D["sync word list into words_&lt;user&gt;_&lt;lang&gt; table"]
    D --> E{"word in JSON file?"}
    E -->|yes, new| F["insert, score 1.0"]
    E -->|yes, existing| G["refresh definition, reactivate"]
    E -->|no longer present| H["deactivate (history kept)"]
    D --> I["practice session (question type per word, by score)"]
    I --> J["session logged to sessions_&lt;user&gt;"]
    J --> K["report --user"]
    D --> M["automatic decay: idle words lose 1.0/week"]
    M --> D
```

## Commands

### Practice

```bash
./lexiloop.sh practice --user bahman --lang german --number 15
```

| Option | Description |
|---|---|
| `--user <name>` | Required. Username (lowercase letters, digits, underscores). |
| `--lang <name>` | Required. Which word list to practice. |
| `--number <n>` | Number of words for the session (default: 20). |
| `--no-audio` | Disable speaking each word aloud. On **macOS**, audio (via `say`) is **on by default**; this flag turns it off. Has no effect on other platforms, where audio is never available. |

Run `./lexiloop.sh practice --help` (or `report`/`init --help`) at any
time to see this same reference from the CLI itself.

#### In-session commands

| Command | Effect |
|---|---|
| `!!` | End the session early and save progress |
| `Ctrl+C` | End the session early and save progress |
| `?` | Repeat: see the word again or replay its audio |
| `+` | Replay the current word's audio |
| `!` | Flag the current word as difficult (score → `1.0`) |
| `@` | Mark the current word as known/mastered (score → `9.0`) |
| `$` | Start a strict 9-repetition drill for the current word (score → `5.0`) |

### Report

```bash
./lexiloop.sh report --user bahman [--lang german]
```

| Option | Description |
|---|---|
| `--user <name>` | Required. Username. |
| `--lang <name>` | Optional. Limit the report to a single word list. Omit to see a separate report for each of the user's word lists. |

Shows a per-day and total summary of sessions, time spent, words practiced,
correct/incorrect/drilled counts, and average time per word. With `--lang`,
this is a single table for that word list; without it, one such table is
printed per language the user has practiced.

### Init

```bash
./lexiloop.sh init --user bahman --lang german
```

| Option | Description |
|---|---|
| `--user <name>` | Required. Username. |
| `--lang <name>` | Required. Language / word list name. |

Creates an empty word list JSON file and its tables for a user/language, if
they don't already exist.

### Help

Every command and flag is also documented in the CLI itself:

```bash
./lexiloop.sh --help
./lexiloop.sh practice --help
./lexiloop.sh report --help
./lexiloop.sh init --help
```

## Project structure

```
lexiloop.py               # main script (single file)
lexiloop.sh                # run through this wrapper, not python3 directly
lexiloop_web.py            # web server (JSON API + static frontend)
lexiloop_web.sh             # run through this wrapper, not python3 directly
web/
  index.html                # frontend markup
  style.css                 # Catppuccin Mocha dark theme
  app.js                     # frontend logic
data/
  lexiloop.db               # SQLite database (auto-created)
  word_lists/
    <user>_<lang>.json      # one word list per user per language
```

## Web UI

LexiLoop also ships with a localhost-only web UI that uses the same
SQLite database and scoring logic as the CLI - standard library only, no
`pip install` or virtualenv needed.

```bash
chmod +x lexiloop_web.sh   # one-time, if not already executable
./lexiloop_web.sh
```

This starts a server at **http://127.0.0.1:9999/** (bound to localhost
only). Open it in a browser for:

- **Practice** - the same Learning/Audio/Meaning question types and growth
  gauge as the CLI, with the same special commands available as buttons
  (`!!` end, `!` flag, `@` master, `$` drill, `?` reveal, `+` replay audio).
  Audio is played via the browser's built-in Web Speech API
  (`speechSynthesis`), so no `say`/macOS dependency is needed.
- **Report** - per-language daily and total summaries, same data as
  `report --user`. Add a language to also see that word list's words,
  current scores/gauges, and per-word practice stats (times practiced,
  correct, incorrect, drilled, flagged, mastered).
- **Word Lists** - see existing `<user>_<lang>` word lists, create new ones
  (equivalent to `init --user --lang`), and edit a list's words and
  definitions directly in the browser - saved straight to
  `data/word_lists/<user>_<lang>.json` and re-synced into the database.
- **About** - an overview of the project and how the CLI and web UI share
  the same database as their single source of truth.

Every page's main button doubles as the `Enter` key shortcut on its input
fields, and pressing `Enter` with a required field empty moves focus there
instead of submitting.

The theme is dark, using the
[Catppuccin Mocha](https://catppuccin.com/palette/) palette.

## Audio / pronunciation (macOS)

On macOS, every word is spoken aloud via the built-in `say` command —
enabled by default, disable with `--no-audio`.

- LexiLoop picks a `say` voice matching `--lang` when one is installed
  (e.g. a German voice for `--lang german`, a French voice for
  `--lang french`), so words are pronounced in their own language rather
  than read with the system default voice's accent. If no matching voice is
  found, the system default voice is used.
- **English** (`--lang english`/`en`) always uses the **system default
  voice** — no `-v` override is applied.
- **German** (`--lang german`/`deutsch`/`de`) prefers the best installed
  "Anna" variant, in order: `Anna (Premium)` > `Anna (Enhanced)` > `Anna`.
  Whichever of these is installed (check with `say -v '?' | grep -i anna`)
  is used.
- For all other recognized languages, LexiLoop falls back to the first
  installed voice matching the locale prefix (e.g. first `fr_FR` voice for
  `--lang french`).
- Recognized `--lang` names for voice matching include `english`, `german`/
  `deutsch`, `french`/`francais`, `spanish`/`espanol`, `italian`, `dutch`,
  `portuguese`, `russian`, `japanese`, `chinese`, `korean`, `turkish`,
  `polish`, `swedish`, `norwegian`, `danish`, `arabic`, or their two-letter
  codes (`en`, `de`, `fr`, ...). Any other `--lang` value still works for
  practice — it just falls back to the default voice for audio.
- **A note on voice quality:** macOS's System Settings -> Accessibility ->
  Spoken Content "Voice 1-4" picks (Siri/personal voices) are *not*
  addressable by name from the command line. This is different from
  downloadable premium voices like `Anna (Premium)`, which **are**
  addressable via `say -v "Anna (Premium)"` and are what LexiLoop uses for
  German when installed.
- During the `$` 9-repetition drill, the word is spoken before **every**
  repetition, not just once — useful for repeated listen-and-spell practice.
- In the "Learning" and "Audio" question types, the word is spoken when the
  prompt appears (before you answer); pressing `?` replays the audio and
  briefly shows the word on screen, in case you need to look at it.

## Color-coded German genders

For words that start with a German article, LexiLoop colors the word
according to its grammatical gender wherever it's displayed:

| Article | Gender | Color |
|---|---|---|
| `der ...` | masculine | blue |
| `die ...` | feminine | red |
| `das ...` | neuter | green |
| (no article / verbs, adjectives, other languages) | — | green |

> **Tip:** as any good German teacher will tell you, always learn a noun
> *together with* its article (`der`/`die`/`das`) and its plural form —
> guessing the gender or plural later is much harder than memorizing them
> from the start. To take advantage of this, write your German nouns in
> `word_lists/<user>_german.json` with the article included, and add the
> plural form as a second, comma-separated form in the same `word` field
> (e.g. `"das Haus, die Häuser"`). Both forms are shown and spoken together,
> and typing either one (singular or plural) counts as correct. The bundled
> `bahman_german.json` list is set up this way as an example, and the color
> coding above will then show you the gender at a glance during practice.

## Requirements

- Python 3 (standard library only, no external dependencies)
- macOS gets spoken-word audio for free (via the built-in `say` command),
  enabled by default. Use `--no-audio` to turn it off. On Linux/Windows,
  audio is simply unavailable (the flag has no effect either way).

## Everyday practice commands

The same `--lang` flag switches the whole session between word lists — use
`--lang german` or `--lang english` (or any other list you've `init`'d) with
any of the commands below. There's just one command: `practice`. The
question type for each word is chosen automatically from its score (see
[How it works](#how-it-works) above), so new words get "Learning" questions,
words you're getting right move to "Audio" then "Meaning" questions, and
words you get wrong (or leave idle) drift back down.

```bash
# Practice session, German
./lexiloop.sh practice --user bahman --lang german --number 15

# Practice session, English
./lexiloop.sh practice --user bahman --lang english --number 15

# Silent session (e.g. in a quiet office) — disables macOS audio
./lexiloop.sh practice --user bahman --lang german --no-audio

# Check today's and overall progress for a language
./lexiloop.sh report --user bahman --lang german

# Check progress across all of a user's word lists
./lexiloop.sh report --user bahman

# Add a new word list (e.g. for a new language or topic)
./lexiloop.sh init --user bahman --lang french
```
