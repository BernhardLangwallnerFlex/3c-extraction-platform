# Cost Model — 3C Extraction Platform

Rough, cautious cost model for the three products (vetcostcheck, BPS, Sanierer). **At launch volumes the fixed infrastructure floor — not LLM tokens — is the majority of the bill.** The per-doc unit cost is ~95% LLM, but actual volumes are low enough (a few hundred docs/month total) that the always-on floor dominates. The cost structure flips back to LLM-dominated only at ~5–20× growth.

> All figures are estimates and err on the high side. The two biggest swing factors are (1) the gpt-5.4 token rate and (2) real pages/doc + photo-page share. Adjust the inputs below and the rest follows.
>
> **Volume note:** the PO's 50 / 100 / 200 figures are **per month**, not per day. Real launch volume is ~350 docs/mo total. This makes the worker pools (which scale to zero) effectively free and leaves the standing infra as the cost driver — see §3.

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
- Window 6am–8pm weekdays → **~22 business days/mo**. At launch volume (a few docs/day per product), worker pools sit cold ~99% of the time.
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

## 3. Monthly cost — volume scenarios (docs/**month**)

The per-page **unit** cost (pure LLM+OCR, ~2.9 ¢ blended) is identical across scenarios. What changes is the **effective** ¢/page — total monthly cost ÷ pages/mo — because the fixed infra floor is amortized over low volume. Pages/mo: Vet 2 pp · BPS 4 pp · Sani 5 pp (e.g. Scenario A = 50×2 + 100×4 + 200×5 = **1,500 pages/mo**).

### Table 1 — current config (API `min-replicas 1`, always warm)

| Scenario | docs/mo (Vet/BPS/Sani) | pages/mo | OCR+LLM | Infra (floor+worker) | **Total/mo** | unit ¢/pg | **eff. ¢/pg** |
|---|---|---|---|---|---|---|---|
| **A — launch** | 50 / 100 / 200 | 1,500 | ~€44 | ~€55 + ~€4 | **~€103** | ~2.9 | **~6.9** |
| **B — 5×** | 250 / 500 / 1,000 | 7,500 | ~€220 | ~€55 + ~€12 | **~€287** | ~2.9 | **~3.8** |
| **C — 20×** | 1,000 / 2,000 / 4,000 | 30,000 | ~€880 | ~€55 + ~€35 | **~€970** | ~2.9 | **~3.2** |

### Table 2 — API scaled to zero (`min-replicas 0`)

Drops the ~€33/mo always-on API replicas; floor becomes Redis €16 + Blob/ACR €8 ≈ **€24**. Trade-off: a few-second cold start on the first upload after idle.

| Scenario | docs/mo (Vet/BPS/Sani) | pages/mo | OCR+LLM | Infra (floor+worker) | **Total/mo** | unit ¢/pg | **eff. ¢/pg** |
|---|---|---|---|---|---|---|---|
| **A — launch** | 50 / 100 / 200 | 1,500 | ~€44 | ~€24 + ~€4 | **~€72** | ~2.9 | **~4.8** |
| **B — 5×** | 250 / 500 / 1,000 | 7,500 | ~€220 | ~€24 + ~€12 | **~€256** | ~2.9 | **~3.4** |
| **C — 20×** | 1,000 / 2,000 / 4,000 | 30,000 | ~€880 | ~€24 + ~€35 | **~€939** | ~2.9 | **~3.1** |

**Read-out:** At launch (Scenario A), the effective cost is ~2.4× the unit cost (6.9 ¢ vs 2.9 ¢), driven entirely by the fixed floor over low volume. Scaling the API to zero brings it to ~4.8 ¢/page (~30% cut) — the single biggest lever while volumes are small. By Scenario B/C the gap collapses (LLM is the majority again), so flip the API back to `min 1` (always-warm) once consistently at Scenario-B levels. Excludes the VetCost UI app (~€12/mo). USD totals are ~+16%.

## 4. Infrastructure detail

- **Standing floor (~€55/mo, flat):** 3× API replicas (`min-replicas 1`, 0.5 vCPU/1 GiB, ~idle) ≈ €33 + Redis Basic C0 ≈ €16 + Blob/ACR ≈ €8. Runs 24/7 regardless of volume. **This is the dominant cost at launch volume.** Setting the API to `min-replicas 0` removes the ~€33 (floor → ~€24) at the cost of a few-second cold start on the first request after idle — see §3 Table 2.
- **Worker compute (volume-driven, near-zero at launch):** 3× worker pools (2 vCPU/4 GiB, `min 0` → scale to zero, `max 5`, KEDA on queue length, 1200s cooldown). At launch volume (a few docs/day per product) the pools are cold ~99% of the time — each doc/batch wakes a replica for seconds, then a 20-min idle cooldown before scaling back to zero ≈ **~€2–5/mo**. Grows to ~€12 (B) → ~€35 (C). One RQ worker = 1 doc at a time; the pool gives parallelism.
- **Peak headroom:** even Scenario-C peaks sit well under one product's 5-worker capacity (~240 docs/hr); if traffic is bursty, raise `max-replicas` 5→10 per product (only billed during the burst).
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

Impact ordering is **volume-dependent**. At launch volume the fixed infra floor dominates, so the token levers barely move the bill; at Scenario B/C they're back on top.

**At launch volume (Scenario A) — attack the fixed floor:**
1. **API `min-replicas 1 → 0`** — saves ~€33/mo (~30% of the total bill at launch), at the cost of a few-second cold start on the first upload after idle. Biggest single lever while volumes are small. Flip back to `min 1` once consistently at Scenario-B levels.

**At growth volume (Scenario B/C) — attack LLM tokens:**
2. **Cut output tokens** — the dominant cost, especially Sanierer. Terser JSON (shorten/normalize `name`, drop `source.snippet` if unused downstream, brief `warnings`) reduces the €12.91/1M side directly.
3. **gpt-5.4-mini** — ~2.8× cheaper (see below); revisit accuracy on dense docs before switching.
4. **Prompt caching** (cached input €0.22 vs €2.16) — only helps if the prompt is reordered so the static instructions/schema form a stable prefix (currently OCR text sits mid-prompt). Input is the smaller half, so modest.
5. **Skip extraction on photo pages** — already the behavior (photo pages aren't part of any subdocument); keep it.

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
