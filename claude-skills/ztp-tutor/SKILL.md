---
name: ztp-tutor
description: >
  Deep reading guide for a single paper already in the Zotero library.
  Writes 5-dimension color highlights with per-sentence Chinese comments,
  figure/table/equation annotations, and a page-1 argument-structure
  overview directly into the Zotero-stored PDF. Original PDF is always
  backed up to a .ztpbak sidecar before any write.
  Trigger on: "论文导读", "/ztp-tutor", "帮我导读", "五维导读",
  "deep reading guide", "tutoring this paper", "给这篇论文做导读",
  "annotate this paper for reading", "帮我精读", "论文精读",
  "reading guide for", "导读一下", "帮我读这篇", "批注这篇论文",
  "阅读引导", "reading assistant", "paper walkthrough", "guided reading".
  For finding and ingesting new papers, use ztp-research instead.
  For synthesizing multiple papers already in the library, use ztp-review instead.
---
# Deep Reading Guide (五维导读)

Reference files live in `references/` next to this file. Read each one only
at the step that names it.

## Language Policy

Detect the user's language from the triggering message and use it for all
user-facing messages. Chinese is the default and is used for all annotation
`comment` fields regardless of the interface language — comments are always
Chinese per-sentence understanding notes (the whole point of the feature).

---

## Step 1 — Resolve the paper

Call `get_paper_for_tutor(title_or_doc_id)` with the title or item key the
user provided.

**If the response contains `needs_disambiguation: true`**, the tool found
multiple candidates. Present them as a numbered list showing `doc_id`,
`title`, `authors`, and `year`. Then ask exactly one question and stop:

> 找到多篇匹配的论文，请告诉我你想导读哪一篇（回复编号）：
>
> 1. [doc_id] 《title》— authors (year)
> 2. …

Wait for the user to pick a single item, then call `get_paper_for_tutor`
again with the selected `doc_id`. Do not proceed until exactly one paper is
confirmed.

**If the tool raises a ToolError** mentioning "no text layer" or "scanned",
tell the user that this PDF has no embedded text layer and OCR is needed
before the reading guide can be written. Stop here.

**If the tool raises any other ToolError**, surface the message verbatim and
stop.

---

## Step 2 — Read the persona and existing annotations

The Step 1 response carries two personalization inputs: `persona`
(`string | null`) and `existing_annotations` (`list`).

### 2a. Persona

**Read `references/persona-parsing.md` now** and apply it to `persona`. It
yields two settings you carry through the rest of the run:

- **English proficiency** — weak / moderate / strong. Only "weak" ENABLES the
  term and long-sentence layers (Steps 4b–4c); anything else suppresses them.
- **Reading depth** — 速览 / 技术细节 / 全面综述. Sets annotation density
  (Step 6). On conflicting hints, choose the MORE CONSERVATIVE density.

**If `persona` is `null`**, ask ONCE, then stop and wait:

> 未检测到阅读画像配置。告诉我你的阅读偏好我会记住，以后不再询问
> （影响批注密度与是否加术语/长难句层）：英文水平 / 领域熟悉度 /
> 导读深度 / 风格偏好。或回复「跳过」用默认（速览 / 中等 / 中等）。

When the user answers, you MUST persist before continuing: call
`save_reading_persona(persona_text=...)` in the format given in
`references/persona-parsing.md` and confirm to the user it was saved. If the
user replies「跳过」/ declines: use the defaults there, do NOT call
`save_reading_persona`, and do not ask again this run.

### 2b. Existing annotations

`existing_annotations` lists the foreign (non-ZotPilot) annotations already
in the PDF. Read it as a personalization signal:

- Treat pages with more than 2 foreign annotation spans as already heavily
  covered by the user. Reduce proposed annotations on those pages — skip
  claims or spans the user has already highlighted.
- Do NOT propose any annotation whose quote clearly overlaps a foreign
  highlight on the same page. The code enforces an IoU > 0.5 rejection gate;
  pre-skip obvious overlaps at this planning stage.
- Foreign annotations do NOT block five-dimension coverage of a page — cover
  the remaining independent understanding points around the user's work.

---

## Step 3 — Plan the annotation list from `page_texts`

Read `page_texts` (the list of `{page_num, text}` objects, one per page).
Use it rather than `sectioned_text` alone: it supplies the `page_num` values
that become `page_hint` in every annotation spec.

**Read `references/annotation-spec.md` now.** It defines the per-annotation
fields, the hard byte caps the tool rejects at its boundary, the exact
mechanism for each element type (Step 4), and the `overview` dict (Step 5).

Cover every dimension the paper actually has, with at least one annotation
each:

- `thesis` — 核心论点 (the central claim the paper defends)
- `concept` — 关键概念 (a key term, definition, or theoretical construct)
- `evidence` — 实证证据 (empirical result, experiment, or supporting data)
- `rebuttal` — 让步反驳 (limitation, counter-argument, or scope boundary)
- `method` — 方法论 (a methodological choice, algorithm step, or design decision)

Skip a dimension only when the paper truly lacks it (e.g., a purely
theoretical paper has no `evidence`). Never duplicate-color the same text
span across two dimensions. Produce a compact list only.

---

## Step 4 — Emit MIXED-kind annotation specs

Following `references/annotation-spec.md` § Element types, emit specs for
every element type present:

- **4a** five-dimension prose claims — always.
- **4b** 关键术语 and **4c** 长难句 — only when Step 2a found English
  proficiency "weak".
- **4d** every entry in `figures[]` — a region note plus a caption highlight.
- **4e** tables — a region note when the table is in `tables[]`; a
  caption-anchored highlight when only `tables_on_page` reports it; nothing
  when no anchor text exists.
- **4f** equations — a highlight on the explanatory sentence, never a region.

Two rules hold everywhere: copy `page` and `bbox` verbatim from `figures[]` /
`tables[]` — never compute, estimate, or synthesize coordinates — and never
use `kind="region"` for an element without an extractor bbox.

---

## Step 5 — Build the `overview` dict

Construct the page-1 argument-structure map in the shape given in
`references/annotation-spec.md` § The `overview` dict: `thesis`, a
five-field `skeleton`, `strongest`, `weakest`. All Chinese, short phrases,
≤ 2000 bytes serialized.

---

## Step 6 — Apply density rules ("满秩 but just-right")

Before calling `annotate_pdf`, review the full list:

- **Span every independent understanding point** — each annotation adds
  understanding the others do not already cover.
- **No redundancy** — two annotations saying the same thing about the same
  span: remove one.
- **Scale to persona depth:** `速览` aims for 8–20 total annotations on a
  typical paper; `技术细节` for 20–50; `全面综述` for every independent
  point, up to the 200 cap.
- **Heavily annotated pages** (> 2 foreign annotations): drop redundant
  annotations there, but do not skip the page entirely.

The byte caps in `references/annotation-spec.md` are hard rejection limits.
Stay well within them.

---

## Step 7 — Pre-skip obvious overlaps with user annotations

Review `existing_annotations` once more against the final list:

- A planned quote that clearly covers the same span as a user highlight on
  the same page: drop it. Pre-skipping avoids a cluttered `unplaced` report
  (`user_already_annotated`).
- Partial overlaps (user highlighted a term; you highlight the full sentence
  containing it) are fine — keep the annotation.
- The page-1 overview sticky-note is exempt; it goes in regardless.

---

## Step 8 — Call `annotate_pdf`

**Pass the annotation payload via a file, not inline.** The list is large;
inline it produces a cluttered, hard-to-read approval prompt.

1. Write the payload to a temp JSON file (use the Write tool), shaped as:
   ```json
   { "annotations": [ ...the final list from Steps 3–7... ],
     "overview":    { ...the dict from Step 5... } }
   ```
   Put it in the OS temp directory so it works on every platform — e.g.
   `$TMPDIR/ztp-tutor-<doc_id>.json` (macOS/Linux) or
   `%TEMP%\ztp-tutor-<doc_id>.json` (Windows). Do NOT hardcode `/tmp`.
2. Call `annotate_pdf` with just:
   ```
   doc_id:     the doc_id from Step 1
   specs_path: the temp JSON file path
   ```
   The approval prompt then shows only the doc_id and the path.

Only fall back to passing `annotations` + `overview` inline if writing a temp
file is not possible in the environment.

The returned dict contains: `placed` (list), `unplaced` (list of
`{label, reason}`), `overview_placed` (bool), `backup_path` (the `.ztpbak`
sidecar), `coverage` (counts by subtype: figures, tables_region,
tables_caption, tables_unanchorable, terms, long_sentences, equations),
`verified` (bool — post-write verification), and `summary` (a pre-formatted
one-line Chinese summary).

---

## Step 9 — Error handling

- **`ScannedPdfError` / "no text layer":** tell the user the PDF lacks an
  embedded text layer and OCR is needed. Do not retry.
- **`annotate_pdf` raises ToolError:** surface the message verbatim. If it
  is "backup failed" or "preflight failed", tell the user the PDF was NOT
  modified.
- **`verified: false`:** tell the user the write verification failed and the
  original PDF was restored from `.ztpbak`. No data was lost.
- **`unplaced` entries:** **read `references/unplaced-reasons.md`** and act
  per reason. Only `ambiguous_multi_match` gets a retry — once, with a
  longer unique span — before it is reported as unplaced.

---

## Step 10 — Coverage summary

Relay a concise one-line Chinese coverage summary built from the `coverage`
dict and the `unplaced` list. Example format:

> 五维齐全 · 5图已标 · 2表（1表仅按标题锚定）· 3术语 · 2长难句 · 备份 foo.pdf.ztpbak · 4处未定位（其中2处用户已批注）

Populate from the actual result:

- **五维** — which dimensions are present; if any are missing say e.g.
  `缺 rebuttal`.
- **N图** — figures with a placed region note.
- **M表** — `tables_region` vs `tables_caption` vs `tables_unanchorable`.
- **K术语 / J长难句** — `coverage.terms` and `coverage.long_sentences`.
- **备份 path** — `backup_path`.
- **X处未定位** — `len(unplaced)`, with `user_already_annotated` broken out
  separately.

If `overview_placed` is false, say the page-1 overview note could not be
placed and suggest the user check page 1.

---

## Safety Note

- The original PDF is always backed up to a `.ztpbak` sidecar file **before
  any byte is written**. If anything fails, the original is restored from
  that backup — it is never consumed or deleted by the rollback process.
- Re-running `/ztp-tutor` on the same paper replaces only the ZotPilot
  annotations from the prior run. Foreign annotations (yours or from other
  tools) are never touched, cleared, or counted.
- The `.ztpbak` file persists after a successful run for manual recovery.
  It is safe to delete once you are satisfied with the reading guide.
