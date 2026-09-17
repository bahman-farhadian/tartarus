# Tartarus Dataset Schema Guide

This document defines the learning-material contract implemented by the current Tartarus codebase.

Tartarus keeps learning material in JSON and learner progress in SQLite. A dataset file therefore describes **what should be learned**; it does not contain score, Leitner, session, Consolidation Track, or Spaced Maintenance state.

---

## 1. Current bundled corpus layout

Shared datasets are stored under:

```text
data/word_lists/<language>/<kind>/<level>/<file>.json
```

Part of speech is encoded in the filename (see section 2), not a directory
level: since each `(language, kind, level, part-of-speech)` group is now a
single file, a directory holding exactly one file added nothing -- list
discovery walks the whole tree regardless of depth (section 14), so this is
purely a corpus-layout simplification, not a contract change.

Current dimensions are:

| Dimension | Current values |
| --- | --- |
| Language | `english`, `german` |
| Kind | `vocabulary`, `sentences` |
| CEFR level | `a1`, `a2`, `b1`, `b2`, `c1`, `c2` |
| Part of speech | `noun`, `verb`, `adjective`, `adverb` |

Current repository snapshot:

```text
84 JSON files
81,216 learning items
42 vocabulary files
42 sentence files
```

Every bundled file currently uses exactly these top-level metadata keys:

```text
name
language
kind
level
```

Every bundled item currently uses exactly:

```text
word
definition
id
```

Every bundled item now carries an explicit, stable `id` (see section 5). This
was a one-time migration that froze each item's previously-derived legacy
identity before the corpus was consolidated from many `partNN` files per
group down to one file per group; new canonical material may still omit
`id` and rely on generated identity, but should prefer writing one
explicitly.

---

## 2. Shared filename convention

The current corpus uses two filename forms: one file per
`(language, kind, level, part-of-speech)` group, holding every item for that
group in frequency/priority order.

### Vocabulary

```text
<language>_<part-of-speech>_<level>.json
```

Examples:

```text
english_noun_a1.json
english_verb_b2.json
german_adjective_c1.json
german_noun_a1.json
```

### Sentences

```text
<language>_sentences_<part-of-speech>_<level>.json
```

Examples:

```text
english_sentences_noun_a1.json
english_sentences_adjective_b2.json
german_sentences_verb_a2.json
german_sentences_noun_b1.json
```

The filename stem is the **list id** used by Tartarus.

Example:

```text
file:    german_noun_a1.json
list id: german_noun_a1
```

### Legacy `partNN` form (historical)

Before the dataset consolidation, each group was split across many numbered
`<...>_partNN.json` files (some groups had 80+ parts). Those files were
merged in part-number order into the single group file above; the merge was
safe because every item already carried an explicit `id` frozen beforehand,
so no item's learner-progress identity depended on which file or array
position it lived at. Any documentation, link, or old branch referencing a
`_partNN.json` filename is describing the pre-consolidation layout.

The Web UI derives Part of Speech from the filename when `metadata.pos` is absent.

---

## 3. Canonical JSON shape

A canonical dataset is one JSON object containing `metadata` and `items`.

```json
{
  "metadata": {
    "name": "German Noun A1",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "das Mal, die Male",
      "definition": "time (as in 'one time';'many times')\nIch war schon viele Male dort."
    },
    {
      "word": "die Zeit, die Zeiten",
      "definition": "time\nIch habe keine Zeit."
    }
  ]
}
```

At runtime the JSON shape is validated without expanding it into type-specific records.

---

## 4. Metadata contract

### `name`

**Type:** string

**Canonical:** required for bundled/new canonical material.

Human-readable list name shown in UI/descriptors.

```json
"name": "German Noun A1"
```

### `language`

**Type:** string

**Canonical values:** `english`, `german` for the bundled corpus.

```json
"language": "german"
```

Custom personal material may technically use another lower-case value, but the current Web category selector is designed around English and German.

### `kind`

**Type:** string

**Canonical values:**

```text
vocabulary
sentences
```

```json
"kind": "vocabulary"
```

The writer normalizes legacy `type` metadata into `kind`. New canonical files should never introduce `type`.

### `level`

**Type:** string

**Canonical bundled values:**

```text
a1 a2 b1 b2 c1 c2
```

Personal material may use `all` when it is not CEFR-specific.

The writer normalizes legacy `cefr_level` metadata into `level`. New canonical files should never introduce `cefr_level`.

### `pos` — optional

**Type:** string

Optional explicit part-of-speech metadata. Bundled files currently omit it because POS is encoded in the path/filename and inferred by the Web descriptor.

Known inferred POS values include:

```text
noun
verb
adjective
adverb
pronoun
preposition
conjunction
interjection
```

If no known POS can be inferred, the Web descriptor uses `all`.

### `ordered` — optional

**Type:** boolean

**Default:** `false`

```json
"ordered": true
```

This marks a list whose source order must be treated explicitly as sequential in modes that consult the ordered flag.

No bundled file in the current repository sets `ordered: true`.

### Unknown metadata

The canonical writer preserves unknown metadata fields instead of deleting them. This lets personal/extensions metadata survive editor round-trips.

---

## 5. Item contract

Each element of `items` is an object.

Canonical minimal item:

```json
{
  "word": "die Zeit, die Zeiten",
  "definition": "time\nIch habe keine Zeit."
}
```

### `word`

**Type:** string

**Required:** yes.

This is the exact target the learner must reproduce.

The validator also reads legacy `text` as a compatibility fallback, but canonical material should write `word`.

### `definition`

**Canonical type:** string; arrays are also accepted by editor/runtime compatibility paths.

The current bundled corpus uses newline-delimited strings.

Runtime normalization converts definition arrays into newline-joined text for practice.

If a legacy record lacks `definition`, the loader can fall back to `translation`, then to the target word; canonical datasets should not rely on those fallbacks.

### `id` — optional in shared material

Every bundled dataset item currently carries an explicit, frozen `id`, so its
identity no longer depends on file path or array position at all. The field
remains optional for material that doesn't set it.

When `id` is absent, Tartarus generates a stable content identity from:

```text
source file path + 1-based item position + word
```

The generated form is:

```text
legacy-<24 hex characters>
```

The generated ID deliberately does **not** include the definition, example, or frequency. Therefore editing only those fields does not create a new learner-progress identity.

However, changing any of these identity coordinates changes the generated ID:

- source file path;
- item position;
- `word` text.

For this reason, moving/reordering/renaming shared items after learners already have progress should be treated as a migration-sensitive operation.

When the Web editor creates a user's first personal copy, the resolved IDs are written explicitly into the personal file and preserved on future saves.

### `word_frequency` — optional

**Type:** non-negative integer

**Default:** `0`

The reader also accepts legacy `frequency` and normalizes it to `word_frequency` when appropriate.

The current bundled corpus does not use numeric frequency fields. Instead, the curated **JSON item order itself is the priority/frequency order** used to introduce equal-score material in normal Tartarus practice.

### Unknown item fields

Unknown item fields are allowed. The Web editor's save path preserves them when the learner changes another field.

This is intentional: the editor should not destroy future/extended schema data it does not understand.

---

## 6. JSON order is pedagogical data

Item order is not cosmetic.

Normal Tartarus selection works as follows for unfinished items:

```text
1. sort by score descending
2. use JSON position as the tie-break for focus-pool membership
3. take at most 16 items
4. shuffle only equal-score groups for presentation
```

For a new dataset every score is `0.0`, so the first 16 JSON items become the initial focus pool.

Therefore dataset authors should order items from the material they want introduced first to the material they want introduced later. In the bundled corpus this is the frequency/priority order.

Do **not** alphabetically re-sort a dataset merely for aesthetics unless that new order is intentionally the learning priority.

---

## 7. Vocabulary multi-form syntax

For vocabulary only, comma-separated forms inside `word` are one target with multiple required forms.

Canonical German noun example:

```json
{
  "word": "das Buch, die Bücher",
  "definition": "book\nDas Buch liegt auf dem Tisch."
}
```

The learner must type that complete literal target exactly. The comma does not split the entry into interchangeable answers.

Accepted:

```text
das Buch, die Bücher
```

Rejected — including reordered forms, partial forms, extra whitespace, and case changes:

```text
die Bücher, das Buch
das Buch,die Bücher
das Buch
die Bücher
Buch
das Buch, die Bücher<space>
das buch, die bücher
```

Matching is the same contract as practice answers (`answer_matches()` in `utils/tartarus.py`): exact equality after Unicode NFC normalization, and nothing else. Outer whitespace is not trimmed. Form order is not ignored. Case, spelling, punctuation, spaces, articles, and commas all matter.

This is the complete noun contract. German nouns are ordinary vocabulary items; Tartarus does not store or generate nominative/accusative/dative/genitive records.

### Do not use comma alternatives in sentence targets

Sentence mode treats the complete `word` as literal text, so commas are sentence punctuation rather than alternative-form separators.

---

## 8. Current bundled definition conventions

The runtime allows multiline definitions and treats them as authored prompt material. The current bundled corpus is highly regular.

### English vocabulary

Exactly two lines:

```text
English definition
English example sentence
```

Example:

```json
{
  "word": "people",
  "definition": "human beings in general\nMany people attended the event."
}
```

### German vocabulary

Exactly two lines:

```text
English meaning
German example sentence
```

Example:

```json
{
  "word": "das Jahr, die Jahre",
  "definition": "year\nEin Jahr hat zwölf Monate."
}
```

Multiple English senses can be separated inside the first line with semicolons when needed.

### English sentences

Exactly two lines:

```text
focus word/phrase
English definition
```

Example:

```json
{
  "word": "Many people attended the event.",
  "definition": "people\nhuman beings in general"
}
```

### German sentences

Exactly three lines:

```text
focus German word/lemma
English meaning
English translation of the sentence
```

Example:

```json
{
  "word": "Ein Jahr hat zwölf Monate.",
  "definition": "das Jahr\nyear\nA year has twelve months."
}
```

These conventions describe the current bundled dataset. They are content-authoring conventions, not separate runtime object types.

---

## 9. Shared material vs personal overrides

Shared corpus files live in the nested hierarchy.

Personal files live directly under `data/word_lists/`:

```text
data/word_lists/<user>_<list-id>.json
```

Example:

```text
data/word_lists/bahman_german_noun_a1.json
```

For user `bahman`, the personal file above overrides the shared list whose id is:

```text
german_noun_a1
```

The shared source remains unchanged and is still visible to other users.

Ownership is determined from the longest valid user-name prefix, so user names that are prefixes of other user names do not leak each other's personal files.

If a personal override was ever created against a pre-consolidation `_partNN`
list id, it must be manually renamed to the new merged list id (and its
content folded into the merged shape) after a dataset consolidation like the
one in section 2 — personal overrides are discovered purely by filename stem,
so an override left under a retired list id silently stops being found.

---

## 10. Personal-list creation

The Word Lists view's "Create a new word list" card (`POST /api/init`, `init_word_list()` in `utils/tartarus_web.py`) can create an empty personal vocabulary list.

The canonical metadata created for an empty personal list is:

```json
{
  "name": "...",
  "language": "unknown",
  "kind": "vocabulary",
  "level": "all"
}
```

No `pos` is written; the Web descriptor treats a missing/unrecognized POS as `all`, so `pos` is optional rather than part of the four-key canonical core.

`/api/init` currently accepts only `vocabulary` as a newly created material type.

Editing an existing shared sentence list can still create a personal sentence override because the original metadata is preserved.

---

## 11. Web editor save contract

The Word Lists editor is designed to be lossless.

When saving:

- shared source bytes are not modified;
- a root-level user override is written;
- existing explicit IDs are preserved;
- generated IDs become explicit on the first personal save;
- item order is preserved;
- multiline definitions are preserved;
- unknown item fields are preserved;
- unknown metadata fields are preserved;
- legacy metadata aliases are normalized to canonical `kind` / `level`;
- writes are atomic through a same-directory temporary file + replacement.

This means an editor change to one definition should not silently erase unrelated schema fields or reset learner identity.

---

## 12. Custom import contract

`save_custom_list()` accepts either:

1. a complete object containing `metadata` and `items`; or
2. a raw items array, which is wrapped in canonical metadata.

For custom imports, every item must already have an explicit stable `id`.

This stricter rule prevents an imported file from receiving identities that depend on an accidental temporary import path/order.

---

## 13. Identifier rules

User names and list ids are sanitized to lowercase and may contain only:

```text
a-z
0-9
_
-
.
!
```

Equivalent regular expression:

```regex
^[a-z0-9_\-\.!]+$
```

Rejected examples include whitespace, path separators, quotes, and traversal syntax.

These identifiers are used in root personal filenames and SQLite table names, so the restriction is part of the persistence contract.

---

## 14. List discovery rules

For a requested `(user, list-id)`, Tartarus resolves material in this order:

1. `data/word_lists/<user>_<list-id>.json` personal override;
2. one unambiguous shared file named `<list-id>.json` anywhere below `data/word_lists/`.

If no shared file exists, lookup fails.

If more than one shared file has the same filename stem, lookup fails as ambiguous rather than choosing one arbitrarily.

This makes globally unique shared filename stems an important dataset requirement.

---

## 15. Source changes and learner progress

`sync_word_list()` maps JSON `content_id` values to SQLite progress rows.

- New content IDs receive new progress rows.
- Existing IDs keep their progress.
- Material no longer present in JSON is marked `active = 0` rather than deleted.
- Reintroduced material with the same ID can reuse its progress row.

For shared no-ID material, remember that changing path, item position, or `word` changes the generated identity. Definition-only edits do not.

### Safe edits after release

Generally safe:

- correcting a definition;
- correcting an example sentence;
- adding an unknown metadata/item field;
- changing optional frequency metadata;
- formatting/indentation changes that do not alter JSON values.

Potentially identity-changing:

- changing `word`;
- reordering existing no-ID items;
- moving/renaming the source file;
- splitting one item into multiple items;
- merging multiple items.

If identity stability matters for existing learners, persist explicit IDs before making structural edits or plan a progress migration.

Since every bundled item now carries a frozen explicit `id` (section 5),
future structural edits to shared material — further splits, merges, or
reordering — are inherently identity-safe and no longer require a freeze
pass first. This guarantee only holds as long as edits preserve existing
items' `id` values; do not regenerate or drop `id` fields when editing.

---

## 16. Formatting recommendations

Tartarus parses normal JSON and does not depend on one-line item formatting. For human review and clean diffs, use:

- UTF-8 without BOM;
- 2-space indentation;
- valid JSON, not JSON5;
- no comments or trailing commas;
- canonical metadata names;
- one logical item per object;
- `\n` inside a JSON string for multiline definitions;
- stable item order.

Recommended style:

```json
{
  "metadata": {
    "name": "German Noun A1",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "die Zeit, die Zeiten",
      "definition": "time\nIch habe keine Zeit."
    },
    {
      "word": "das Jahr, die Jahre",
      "definition": "year\nEin Jahr hat zwölf Monate."
    }
  ]
}
```

---

## 17. Validation checklist for a new shared dataset

Before adding a file, verify all of the following:

- [ ] File is under `data/word_lists/<language>/<kind>/<level>/`.
- [ ] Filename follows the vocabulary or sentences convention (section 2) — one file per group, no `_partNN` suffix.
- [ ] Filename stem is globally unique under `data/word_lists/`.
- [ ] `metadata` is an object.
- [ ] `metadata.name` is meaningful.
- [ ] `metadata.language` matches the directory language.
- [ ] `metadata.kind` is `vocabulary` or `sentences` and matches the directory.
- [ ] `metadata.level` matches the directory level.
- [ ] `items` is an array.
- [ ] Every item has a non-empty `word`.
- [ ] Every canonical item has a meaningful `definition`.
- [ ] Item order represents intended learning/frequency priority.
- [ ] Multi-form vocabulary targets include every form the learner must type.
- [ ] Sentence commas are ordinary punctuation.
- [ ] No duplicate explicit IDs exist if IDs are supplied.
- [ ] Existing no-ID items are not casually reordered after learner progress exists.
- [ ] File is UTF-8 and valid JSON.

Then run the unified suite:

```bash
PYTHONPYCACHEPREFIX=/tmp/tartarus-pycache \
python3 utils/test_tartarus.py -v
```

---

## 18. Schema examples by dataset family

### English vocabulary

```json
{
  "metadata": {
    "name": "English Noun A1",
    "language": "english",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "people",
      "definition": "human beings in general\nMany people attended the event."
    }
  ]
}
```

### German vocabulary noun

```json
{
  "metadata": {
    "name": "German Noun A1",
    "language": "german",
    "kind": "vocabulary",
    "level": "a1"
  },
  "items": [
    {
      "word": "das Buch, die Bücher",
      "definition": "book\nDas Buch liegt auf dem Tisch."
    }
  ]
}
```

### English sentence

```json
{
  "metadata": {
    "name": "English Sentences Noun A1",
    "language": "english",
    "kind": "sentences",
    "level": "a1"
  },
  "items": [
    {
      "word": "Many people attended the event.",
      "definition": "people\nhuman beings in general"
    }
  ]
}
```

### German sentence

```json
{
  "metadata": {
    "name": "German Sentences Noun A1",
    "language": "german",
    "kind": "sentences",
    "level": "a1"
  },
  "items": [
    {
      "word": "Ein Jahr hat zwölf Monate.",
      "definition": "das Jahr\nyear\nA year has twelve months."
    }
  ]
}
```

---

## 19. What does not belong in dataset JSON

Do not store learner progress inside material files.

These belong to SQLite, not JSON:

```text
score
last_practiced
last_tartarus_completed
active
times_practiced
times_correct
times_incorrect
times_drilled
times_mastered
leitner_box
leitner_last_reviewed
consolidation_step
leitner_maintenance_streak
pending_drills
practice_bucket
mastery_events
session history
```

Do not add the removed review-era concepts as material requirements:

```text
noun_forms
noun_case
drill_pending
times_flagged
stage_reached
```

The material model is intentionally simple: a stable target, its prompt/definition, canonical metadata, and source order.
