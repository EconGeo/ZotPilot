# Annotation spec — `ztp-tutor` Steps 3–5

The shape of every annotation, the hard caps the tool enforces, the exact
`kind` / `subtype` / coordinate mechanism for each element type, and the
page-1 `overview` dict. None of these are judgment calls.

## Per-annotation fields

```
{
  quote:      the verbatim excerpt from the paper text (≤ 1000 bytes UTF-8),
  dimension:  one of: thesis | concept | evidence | rebuttal | method,
  comment:    Chinese per-sentence understanding note (≤ 500 bytes UTF-8),
  page_hint:  the page_num of the page where this quote appears (1-based),
  kind:       "highlight" for all prose/term/equation/caption annotations,
              "region"    for figure and materialized-table region notes,
  subtype:    one of: dim | term | long_sentence | figure | figure_caption |
                      table | equation   (informational; drives coverage report),
  page:       required when kind="region" — copy verbatim from figures[]/tables[],
  bbox:       required when kind="region" — copy verbatim from figures[]/tables[]
}
```

## Hard byte caps (rejected at the tool boundary)

- `comment` ≤ 500 bytes (UTF-8)
- `quote` ≤ 1000 bytes (UTF-8)
- Total annotations ≤ 200
- `overview` total ≤ 2000 bytes

These are hard rejection limits, not soft suggestions. Stay well within them.
Never reproduce large blocks of text — compact list only.

## Element types

### 4a. Five-dimension prose claims
- `kind="highlight"`, `subtype="dim"`, `dimension` = the matching key.
- `quote` is the verbatim sentence or clause that carries the claim.
- `page_hint` = the `page_num` from `page_texts` where the quote appears.
- Do not invent a `page` or `bbox` field on highlight annotations.

### 4b. 关键术语 (English proficiency "weak" only)
- `kind="highlight"`, `subtype="term"`, `dimension="concept"`.
- Short quote of the term itself (≤ 40 characters preferred).
- `comment` = brief Chinese gloss: what the term means in this paper's context.
- `page_hint` from the page where the term first appears.

### 4c. 长难句 (English proficiency "weak" only)
- `kind="highlight"`, `subtype="long_sentence"`, `dimension="method"` or
  `"concept"` as appropriate.
- Quote the full difficult sentence (≤ 200 characters preferred).
- `comment` = grammar skeleton + Chinese translation in natural prose.
- `page_hint` from the page where the sentence appears.

### 4d. 图 Figure — for EVERY entry in `figures[]`
All entries are guaranteed to have `bbox` and `caption` from the extractor.

**Region note** (the primary anchor at the figure):
- `kind="region"`, `subtype="figure"`.
- `page` = `figure.page_num` — copy verbatim, do not edit.
- `bbox` = `figure.bbox` — copy the four-element list verbatim. Never
  compute, estimate, or modify a bbox.
- `dimension` = `"evidence"` (figures are usually evidence or method; use
  your judgment but do not leave blank).
- `comment` = 该图导读: one or two Chinese sentences on what this figure
  shows and why it matters to the argument.
- `quote` = empty string `""`.

**Caption highlight** (secondary anchor on the caption text):
- `kind="highlight"`, `subtype="figure_caption"`.
- `quote` = `figure.caption` (the full caption string, truncated to 1000
  bytes if needed).
- `page_hint` = `figure.page_num`.
- `comment` = the same brief 导读 as the region note, or a complementary note.
- If the caption is very short (< 12 chars), omit the caption highlight and
  keep only the region note.

### 4e. 表 Table

**If the table is in `tables[]`** (materialized, bbox present):
- Emit a region note exactly as in 4d, using `table.bbox`, `table.page_num`,
  `subtype="table"`.
- Also emit a caption highlight for `table.caption` with `subtype="table"`,
  `page_hint=table.page_num`. If the caption is null or very short, omit it.

**If a page has `tables_on_page[page_num] > 0` but no entry in `tables[]`**
(detected but not materialized — no bbox available):
- Emit a text-anchored highlight on the caption text or the nearest "Table N"
  label you can locate in `page_texts`, `subtype="table"`, `kind="highlight"`.
- If no caption or "Table N" text can be found in the page text, emit
  nothing for this table. It will appear in `unplaced` as
  `unanchorable_table`.
- NEVER synthesize a `bbox` for a table that is not in `tables[]`.

### 4f. 公式 Equation
- `kind="highlight"`, `subtype="equation"`.
- Quote the SPECIFIC explanatory sentence that describes or derives the
  equation (the prose adjacent to the equation, not the equation glyphs
  themselves). Choose a sentence ≥ 12 characters.
- `comment` = Chinese explanation of what the equation means and how it
  connects to the argument.
- Do NOT use `kind="region"` for equations — there is no extractor bbox.
- If the explanatory sentence appears more than once on the page, the code
  reports `ambiguous_multi_match`; use a longer surrounding sentence that is
  unique.

## The `overview` dict (page-1 sticky-note)

```json
{
  "thesis":    "核心论点，一句话",
  "skeleton": {
    "question":   "研究问题",
    "claim":      "主要论点",
    "evidence":   "关键证据",
    "rebuttal":   "让步/局限",
    "conclusion": "结论"
  },
  "strongest": "最有力的论据",
  "weakest":   "最薄弱的环节"
}
```

All fields in Chinese, short phrases. Total JSON serialized to ≤ 2000 bytes.
