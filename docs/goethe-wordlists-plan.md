# Goethe Vocabulary Dataset Plan

The 78,000-entry noun corpus is a broad NLP lexicon, not a practical learner
curriculum.  Tartarus will use the official Goethe vocabulary lists as the
authoritative beginner/intermediate source instead.

## Stages

1. Extract the text layer from the official A1, A2, and B1 PDFs into separate,
   deduplicated Tartarus JSON lists.
2. Review extraction counts and representative entries, especially article and
   plural notation and wrapped PDF lines.
3. Join the extracted German entries with the existing learner deck for English
   translations and bilingual examples. Goethe remains the source of the CEFR
   level; no per-word LLM request is needed for this step.
4. Add only the resulting learner-sized lists to the app after validation. Keep
   raw source PDFs and generated output separate so regeneration is reproducible.

The committed PDFs are retained as the authoritative source for the generated
snapshots. They contain an embedded text layer and were processed with
`pdftotext -raw`; no OCR or local model was used. Goethe plural notation is
expanded where possible, for example `der Bahnhof, ¨-e` becomes
`der Bahnhof, die Bahnhöfe`.

The generated JSON files are reviewed snapshots. Regeneration is intentionally
outside the application repository until the extraction process is needed
again.

The audit reports noun coverage, expanded plural coverage, and entries whose
lemma cannot be found in the source PDF text.
