# Per-Document Cost Breakdown

Based on: VCC_Viele_Dokumente.pdf (6 pages, 4 invoices), gpt-5.4, dual OCR (Mistral + Azure Doc Intelligence).

| Step | Cost | % of total |
|------|------|------------|
| OCR (Mistral $0.006 + Azure $0.009) | $0.015 | 7.9% |
| Analysis (1 LLM call, 9.6K in / 626 out) | $0.033 | 17.6% |
| Extraction (4 LLM calls, 17.7K in / 6.5K out) | $0.141 | 74.5% |
| **Total** | **$0.190** | |
| Per page | $0.032 | |

Extraction dominates at 74.5% — driven by gpt-5.4's $15/1M output tokens. Invoice 3 alone (22 items, 4K output tokens) costs $0.077. For a typical 1-2 page single-invoice doc, expect ~$0.03-0.06.

## vs. old pipeline (LandingAI + gpt-4o)

| Step | Old | New | Change |
|------|-----|-----|--------|
| OCR | $0.180 | $0.015 | -92% |
| Analysis | $0.030 | $0.033 | +10% (images added) |
| Extraction | $0.109 | $0.141 | +30% (gpt-5.4 output costs more) |
| **Total** | **$0.319** | **$0.190** | **-41%** |

Net savings of 41% despite gpt-5.4's higher output token price, because OCR cost collapsed (12x reduction from LandingAI to dual Mistral+Azure).

## gpt-5.4 vs gpt-5.4-mini (estimated)

Same token counts, different pricing. Mini has not been tested in production with the current pipeline (dual OCR + improved prompt + item count hint).

| Step | gpt-5.4 | gpt-5.4-mini | Diff |
|------|---------|--------------|------|
| OCR | $0.015 | $0.015 | same |
| Analysis | $0.033 | $0.010 | 3.3x less |
| Extraction | $0.141 | $0.042 | 3.3x less |
| **Total** | **$0.190** | **$0.067** | **2.8x less** |
| Per page | $0.032 | $0.011 | |

Mini would be ~1 cent/page vs 3 cents/page. Previous benchmarking showed consistency issues (9 vs 20 items extracted on one test doc), but that was before the dual OCR and prompt improvements. Worth revisiting if cost becomes a concern at scale.
