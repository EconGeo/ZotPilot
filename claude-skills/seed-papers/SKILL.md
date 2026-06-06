---
name: seed-papers
description: >
  Seed a project's bibliography from the local Zotero library before running /discover lit.
  Trigger on: "seed papers", "search my library first", "seed the bibliography",
  "check what I already have on X", "pre-search my library",
  or at the start of /discover lit when ZotPilot is available.
  Outputs: bibliography_base.bib (BibTeX entries) + zotero_seed.md (annotation summaries).
  Run BEFORE /discover lit so the librarian agent knows what's already covered.
allowed-tools: Read,Write,Edit,Grep,Glob
---

# Seed Papers — Pre-search Local Library Before Literature Review

Seeds a project's `bibliography_base.bib` from the local Zotero ChromaDB index.
The librarian agent reads this file automatically at the start of `/discover lit`,
so running this skill first prevents duplicated search effort and surfaces anchor
papers the user already knows.

**Architecture note:** The librarian agent has no MCP tools and cannot query ChromaDB
directly. This skill (running in the main session with MCP access) is the bridge:
  ZotPilot/ChromaDB → (this skill, main session) → `bibliography_base.bib` → librarian reads

---

## Input

`$ARGUMENTS` — optional topic query string. If omitted, read the research question
from `.claude/references/domain-profile.md` (field: Research Question) or ask the user.

---

## Workflow

### Step 1: Resolve Topic

If `$ARGUMENTS` is non-empty, use it as the primary query.
Otherwise:
- Try reading `.claude/references/domain-profile.md` for the research question
- If not found, ask: "What is the core topic for this literature search?"

Identify:
1. **Primary query** — the core topic (e.g., "zoning regulation housing affordability")
2. **Secondary query** — a mechanism or angle (e.g., "upzoning statewide preemption reform")

### Step 2: Check Existing Seed

Check whether `bibliography_base.bib` already exists and has entries.
If it does, read it and note which doc_ids are already present (from `% doc_id:` comment lines).
Report: "Found N papers already in bibliography_base.bib — will skip duplicates."

### Step 3: Search Local Library

Run two searches using `mcp__zotpilot__search_topic`:
1. Primary query — `num_papers: 20, verbosity: standard`
2. Secondary query — `num_papers: 10, verbosity: standard`

Merge results, deduplicate by `doc_id`. Remove any doc_ids already in `bibliography_base.bib`.

### Step 4: Present Candidates

Show a ranked table:

```
| # | Title (Year) | Journal | Author(s) | Relevance |
|---|-------------|---------|-----------|-----------|
| 1 | ... | ... | ... | [brief phrase] |
```

Sort by relevance (most relevant first). Include all merged results.

Ask: "Which papers do you want to include? Reply with row numbers (e.g. `1,3,5`),
`all`, or `skip` to cancel."

**Stop here. Wait for user response before proceeding.**

### Step 5: Fetch Paper Details

For each confirmed paper, call `mcp__zotpilot__get_paper_details` with its `doc_id`.
Collect: title, authors, year, journal/venue, DOI, abstract, Zotero key.

### Step 6: Write BibTeX to bibliography_base.bib

Append (or create) `bibliography_base.bib` at the project root.

Format:
```bibtex
% doc_id: <ZOTERO_KEY>
@article{<citekey>,
  author  = {<authors>},
  title   = {<title>},
  journal = {<journal>},
  year    = {<year>},
  doi     = {<doi>},
  note    = {Zotero: <ZOTERO_KEY>. <1-sentence relevance note.>}
}
```

Citekey convention: `<firstauthor_lastname><year><first_content_word>` — all lowercase,
no spaces (e.g., `gyourko2021impact`, `howard2021rent`).

Group entries by thematic section with `% ──` divider comments.

If the file already exists, append new entries after the last existing entry.
Do NOT rewrite existing entries.

### Step 7: Write Annotation Summary

Create or append `quality_reports/literature/{project}/zotero_seed.md`.

The `{project}` folder name is the project root directory name (e.g., `zoning2026`).
Create the directory if it doesn't exist.

For each included paper, write:

```markdown
### Author (Year) — Short Title
- **Zotero ID:** <key> | **DOI:** <doi>
- **Journal:** <journal>
- **Method:** <1-line empirical method>
- **Key finding:** <1-2 sentence main result>
- **Relevance:** <why this matters for the current project>
```

End the file with a `## Notes for Librarian` section summarizing:
- Which subfields are already well-covered (no need to extend)
- Where the web search should focus (gaps, recent work, reform effects)
- Any scooping risks to watch for

### Step 8: Report and Hand Off

Report:
- N papers written to `bibliography_base.bib`
- File location of `zotero_seed.md`
- Summary of coverage by theme

Then say:
> "Run `/discover lit [topic]` next. The librarian will read `bibliography_base.bib`
> automatically and extend outward from these anchor papers."

---

## Edge Cases

**ZotPilot not indexed:** If `search_topic` returns an error or empty results,
say: "ZotPilot index not found or empty. Run `/ztp-setup` to index your library,
then re-run `/seed-papers`. Alternatively, run `/discover lit` directly — the librarian
will search from scratch."

**No papers confirmed by user:** Write an empty `bibliography_base.bib` with a
header comment explaining it was created by `/seed-papers` with no papers selected.
Still proceed to Step 8.

**bibliography_base.bib already contains all candidates:** Report "All candidates
already in bibliography_base.bib — nothing new to add." Skip Steps 5-7.
