# Cost Model — 3C Extraction Platform

Rough, cautious cost model for the three products (vetcostcheck, BPS, Sanierer). Cost is **~95% LLM tokens**; OCR is minor; infrastructure is near-negligible at expected volumes. Everything scales ~linearly with volume, so you can interpolate.

> All figures are estimates and err on the high side. The two biggest swing factors are (1) the gpt-5.4 token rate and (2) real pages/doc + photo-page share. Adjust the inputs below and the rest follows.

## 1. Pricing inputs (the knobs)

**LLM — Azure OpenAI `gpt-5.4`, GlobalStandard (<272k context), Germany West Central** (Azure Foundry list price, 2026-06):

| | per 1M tokens (EUR) | per 1M tokens (USD, ÷0.8601) |
|---|---|---|
| Input | €2.16 | ~$2.51 |
| Cached input | €0.22 | ~$0.26 |
| Output | €12.91 | ~$15.01 |

Output is ~6× input — **output tokens dominate the bill.** (Code: `core/processors/azure_processor.py` `PROMPT_RATE`/`COMPLETION_RATE` are set to the USD per-1K equivalents; cached input is not separately tracked.)

**OCR — DualOCR runs both engines on every page** (incl. photo pages): Mistral ~$0.001/page + Azure Document Intelligence Read ~$0.0015/page ≈ **$0.0025/page (~0.25 ¢)**.

**Assumptions (adjust to taste):**
- Window 6am–8pm weekdays → **~22 business days/mo**.
- Tokens per *content* page: ~6K input + ~1.5K output (from production logs).
- Avg pages/doc & photo share: **VetCost** 2 pp / 0% · **BPS** 4 pp / ~35% · **Sanierer** 5 pp / ~10%.

## 2. Unit economics

Two page types (photo-only pages are OCR'd and included in the analyze call but get **no extraction call**):

| Page type | ~Cost |
|---|---|
| Content page (invoice/quote text) | **~3.5 ¢** (OCR + analyze share + extraction) |
| Photo-only page | **~0.6 ¢** (OCR + image in analyze; no extraction) |

**Per document** and **blended per page** (cautious, EUR):

| Product | €/page (blended) | €/doc |
|---|---|---|
| VetCost | ~3.0 ¢ | ~6 ¢ |
| BPS | ~2.3 ¢ | ~9 ¢ |
| Sanierer | ~3.2 ¢ | ~16 ¢ |

- **BPS is cheaper per page** only because ~35% of its pages are photo-only (OCR but no extraction); its content pages still cost ~3.5 ¢.
- **Sanierer is the most expensive per doc** — its dense LV tables (17–41 line items) produce ~5–6K **output** tokens/doc, and output is the €12.91/1M term.

## 3. Monthly cost — volume scenarios

| Scenario | docs/day (Vet/BPS/Sani) | OCR+LLM | Infra (+peak) | €/page (Vet / BPS / Sani) |
|---|---|---|---|---|
| **A — baseline** | 50 / 100 / 200 | ~€970 | ~€90 | ~3.0 ¢ / ~2.3 ¢ / ~3.2 ¢ |
| **B — +500 each** | 550 / 600 / 700 | ~€4,380 | ~€280 | ~3.0 ¢ / ~2.3 ¢ / ~3.2 ¢ |
| **C — +1000 each** | 1,050 / 1,100 / 1,200 | ~€7,790 | ~€430 | ~3.0 ¢ / ~2.3 ¢ / ~3.2 ¢ |

(€/page is a unit cost — identical across scenarios. USD totals are ~+16%.)

## 4. Infrastructure detail

- **Standing (~€55/mo, flat):** 3× API replicas (`min-replicas 1`, 0.5 vCPU/1 GiB, ~idle) ≈ €33 + Redis Basic C0 ≈ €16 + Blob/ACR ≈ €8. Runs 24/7 regardless of volume.
- **Worker compute (volume-driven):** 3× worker pools (2 vCPU/4 GiB, `min 0` → scale to zero off-hours, `max 5`, KEDA on queue length). ~€35 (A) → ~€300 (C). One RQ worker = 1 doc at a time; the pool gives parallelism.
- **Peak buffer:** added cautiously to B/C. Even Scenario-C peaks sit well under one product's 5-worker capacity (~240 docs/hr); if traffic is bursty, raise `max-replicas` 5→10 per product (only billed during the burst).
- Excludes `ca-vetcostcheck-ui` (~€12/mo).

## 5. Per-document breakdown (worked example)

Based on `VCC_Viele_Dokumente.pdf` (6 pages, 4 invoices), gpt-5.4, DualOCR:

| Step | Cost | % |
|------|------|---|
| OCR (Mistral $0.006 + Azure $0.009) | $0.015 | 7.9% |
| Analysis (1 LLM call, 9.6K in / 626 out) | $0.033 | 17.6% |
| Extraction (4 LLM calls, 17.7K in / 6.5K out) | $0.141 | 74.5% |
| **Total** | **$0.190** | |
| Per page | $0.032 | |

Extraction dominates (74.5%), driven by the $15/1M output rate. Invoice 3 alone (22 items, 4K output tokens) costs $0.077. A typical 1–2 page single-invoice doc ≈ $0.03–0.06.

## 6. Cost levers (in order of impact)

1. **Cut output tokens** — the dominant cost, especially Sanierer. Terser JSON (shorten/normalize `name`, drop `source.snippet` if unused downstream, brief `warnings`) reduces the €12.91/1M side directly.
2. **gpt-5.4-mini** — ~2.8× cheaper (see below); revisit accuracy on dense docs before switching.
3. **Prompt caching** (cached input €0.22 vs €2.16) — only helps if the prompt is reordered so the static instructions/schema form a stable prefix (currently OCR text sits mid-prompt). Input is the smaller half, so modest.
4. **Skip extraction on photo pages** — already the behavior (photo pages aren't part of any subdocument); keep it.

## 7. gpt-5.4 vs gpt-5.4-mini (estimated)

Same token counts, different pricing. Mini not yet validated with the current pipeline (DualOCR + hardened prompt).

| Step | gpt-5.4 | gpt-5.4-mini | Diff |
|------|---------|--------------|------|
| OCR | $0.015 | $0.015 | same |
| Analysis | $0.033 | $0.010 | ~3.3× less |
| Extraction | $0.141 | $0.042 | ~3.3× less |
| **Total** | **$0.190** | **$0.067** | **~2.8× less** |
| Per page | $0.032 | $0.011 | |

Mini ≈ 1 ¢/page vs ~3 ¢/page. Earlier benchmarking showed consistency issues (e.g. 9 vs 20 items extracted), but that predates the DualOCR + prompt improvements — worth revisiting if cost matters at scale.

## 8. vs. old pipeline (LandingAI + gpt-4o)

| Step | Old | New | Change |
|------|-----|-----|--------|
| OCR | $0.180 | $0.015 | −92% |
| Analysis | $0.030 | $0.033 | +10% (images added) |
| Extraction | $0.109 | $0.141 | +30% (gpt-5.4 output costs more) |
| **Total** | **$0.319** | **$0.190** | **−41%** |

Net −41% despite gpt-5.4's higher output price, because OCR cost collapsed (~12× cheaper moving from LandingAI to DualOCR).
