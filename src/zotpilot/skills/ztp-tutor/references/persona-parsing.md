# Persona parsing — `ztp-tutor` Step 2a

`persona` is the raw text of the `## 阅读画像 (Reading Persona)` section from
`~/.config/zotpilot/ZOTPILOT.md`, or `null` if absent. Parse it by
**intent-matching, not literal enum**: match each hint leniently against the
raw section text.

## English proficiency — gates the term + long-sentence layers

| Verdict | Matches | Effect on Steps 4b–4c |
|---|---|---|
| **weak** | `英文弱`, `英文不好`, `英文一般`, `入门`, `中等`, `poor`, `intermediate`, `beginner`, `basic`, `不太好`, `一般般` | ENABLE the term and long-sentence layers |
| **strong** | `高级`, `expert`, `advanced`, `proficient`, or similar strength signals | SUPPRESS both layers entirely |
| **moderate** (default) | no match | SUPPRESS both layers — do not add extra layers unless clearly warranted |

## Reading depth — gates annotation density (Step 6)

| Depth | Matches | Density |
|---|---|---|
| `速览` (default) | `速览`, `quick read`, `overview`, `skim` | sparse: thesis claim + key figures + 1–2 annotations per present dimension |
| `技术细节` | `技术细节`, `technical`, `detailed`, `in-depth`, `深度` | standard: fuller coverage of method, evidence, and proof steps |
| `全面综述` | `全面综述`, `comprehensive`, `thorough`, `全面`, `详尽` | maximal: annotate every independent understanding point within the hard caps |

When hints conflict (e.g., `速览` but `新手`), favor the MORE CONSERVATIVE
density to avoid over-annotation. When depth cannot be recognized, default
to `速览`.

## Persisting a persona the user just gave (`persona` was `null`)

Call `save_reading_persona(persona_text=...)` with the four hints formatted
as markdown lines:

```
- 英文水平：入门
- 领域熟悉度：中等
- 导读深度：速览
- 风格偏好：结构化要点
```

This writes the `## 阅读画像 (Reading Persona)` section to
`~/.config/zotpilot/ZOTPILOT.md` so the NEXT `/ztp-tutor` run auto-detects it
and does **not** ask again. The tool returns `{saved, path, action}` — confirm
to the user it was saved. Failing to persist is exactly why a user gets
re-asked every run.

Defaults when the user declines (「跳过」): sparse density, English
proficiency moderate, no term/long-sentence layer. Do not call
`save_reading_persona` and do not ask again this run.
