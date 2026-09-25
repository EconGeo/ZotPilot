# `unplaced` reasons — `ztp-tutor` Step 9

`annotate_pdf` returns `unplaced` as a list of `{label, reason}`. Act per
reason:

| reason | action |
|--------|--------|
| `ambiguous_multi_match` | Try a longer, more unique span from the same sentence; retry the single annotation once, then report it as unplaced |
| `too_short` | Note to user — the quote was below the 12-char minimum; the code rejected it |
| `no_match` | Note to user — the exact text could not be found in the PDF |
| `user_already_annotated` | Note to user — they already covered this point; skipped to avoid duplication |
| `region_clustered` | The figure/table sticky-note could not find a clear spot near existing annotations; a caption highlight was used instead |
| `unanchorable_table` | No bbox available and no caption text found; the table could not be anchored |
| `page_density_exceeded` | The page already has many annotations; this item was deferred |

In the Step 10 summary, report the total `len(unplaced)` and break out the
`user_already_annotated` count separately, so the user understands their
existing annotations were respected.
