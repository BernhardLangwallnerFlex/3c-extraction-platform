# Cost Model — 3C Extraction Platform

Reworked 2026-10-05 from **measured** data (Azure Cost Management, Azure Monitor metrics, worker logs for Sep 7 – Oct 5 2026), replacing the 2026-08-12 assumption-based model. All amounts EUR, excl. VAT.

**Headline:** infrastructure, not tokens, is the main cost. OCR and LLM are each only 1–2 ¢/page, so optimising either needs a quality or reliability reason, not just cost. Per-document cost falls steeply with volume.

## 1. Indicative cost for 3C (shared with 3C 2026-10-05)

Volumes as planned: 50 VCC / 100 BPS / 200 Sanierer docs per month (350 total). Infra €120/mo (assumes workers scale to zero — see §4) split equally per document = €0.34/doc.

| Product | Pages/doc | Infra €/doc | OCR €/doc | LLM €/doc | **All-in €/doc** | **All-in €/page** |
|---|---|---|---|---|---|---|
| VCC | ~5.1 | 0.34 | 0.06 | 0.10 | **0.50** | **0.10** (infra 0.067 · OCR 0.012 · LLM 0.019) |
| BPS | ~15.8 | 0.34 | 0.21 | 0.10 | **0.65** | **0.04** (infra 0.022 · OCR 0.013 · LLM 0.006) |
| Sanierer (est.) | ~5 | 0.34 | 0.06 | 0.11 | **0.50** | **0.10** (infra 0.069 · OCR 0.012 · LLM 0.022) |

- Sanierer has **zero production jobs**, so its row is an estimate (5 pp, ~8K analyze in, ~10K extraction in / 5K out).
- Allocating infra **per page** instead of per document gives €0.042/page for all products → all-in VCC ~0.07, BPS ~0.06, Sanierer ~0.08 €/page. Use per page if 3C is billed per page.
- Volume effect (all-in €/doc at 350 / 1,750 / 7,000 docs/mo): VCC 0.50 / 0.25 / 0.20 · BPS 0.65 / 0.40 / 0.35 · Sanierer 0.50 / 0.26 / 0.21.
- 3C-facing doc: https://claude.ai/code/artifact/949a51c2-41d1-46b7-9087-c775d73f5723

## 2. Unit prices (observed 2026-10-05, Azure Retail Prices API, germanywestcentral)

| Item | Price | Note |
|---|---|---|
| gpt-5.4 GlobalStandard | €2.20 in / €0.22 cached / €13.20 out per 1M | Billed effective €2.15–2.20 / €12.88–13.20. Code `PROMPT_RATE`/`COMPLETION_RATE` ($2.51/$15.01) ~2% low — fine. |
| gpt-5.6-terra | $2.00 / $0.20 / $12.00 per 1M (+ cache write $2.50) | Now **20% cheaper than 5.4** (price cut 2026-08-01); the "+10% cost" argument against Terra is gone. |
| gpt-5.4-mini | $0.75 / $4.50 per 1M | |
| Azure Document Intelligence **Layout** (`prebuilt-layout`, what we call) | €8.80 / 1K pages | Read would be €1.32 / 1K. |
| Mistral OCR (`mistral-ocr-latest`) | $4 / 1K pages (OCR 4.1) | Alias target undocumented; OCR 3 was $2/1K. Billed outside Azure — check the Mistral invoice. |
| Container Apps consumption | idle €2.6e-6 per vCPU-s and per GiB-s; active vCPU €2.1e-5 /s | A 4 vCPU / 8 GiB replica ≈ €80/mo idle. |
| Redis Standard C0 | €34/mo billed | (`$40.15` is the USD list price.) Azure Cache for Redis retires 2028-09-30; Azure Managed Redis B0 HA ≈ €24/mo is the successor. |

## 3. Measured variable cost (prod, Sep 7 – Oct 5 2026)

| | BPS | VCC |
|---|---|---|
| Jobs/month | ~390 | ~56 |
| Pages/doc (median / p90 / max) | 14 / 28 / 94 | 4 / 11 / 17 |
| Sub-docs/doc (median / p90) | 1 / 3 | 1 / 3 |
| Analyze tokens/doc (in / out) | 15.8K / 87 | 8.2K / 347 |
| Extraction tokens/doc (in / out) | 11.7K / 2.6K | 11.1K / 3.9K |
| LLM €/doc | 0.096 | 0.099 |
| OCR €/doc | 0.193 (+ ~0.02 wasted on failed runs) | 0.061 |

- **OCR = Document Intelligence (€0.0088/page) + Mistral (€0.0034/page) ≈ €0.012/page**, both engines on every page incl. photo pages. For BPS, OCR is ~⅔ of variable cost.
- BPS LLM/page is low because ~⅓ of pages are photos (OCR'd, included in analyze, no extraction call).
- **Retry/failure overhead (BPS): ~+7%.** 12 of 385 runs failed (3.1%), 10 of them >50 pages on the analyze `Too many images in request: 51, maximum allowed: 50` — after full DualOCR. Content-filter fallback 0.5% of analyze calls. VCC: none.
- Totals (all tiers + local experiments): gpt-5.4 Sep 12.1M in / 1.3M out = €43; DocIntel Sep 7,508 billed pages = €64.

## 4. Infrastructure (Cost Management, actual)

| | Aug | Sep | Oct 1–5 |
|---|---|---|---|
| **Total (3C RGs + DocIntel)** | €462 | €702 | €93 |
| Workers prod (3) | €130 | €244 | |
| Workers test (3) | €125 | €240 | |
| APIs + VCC UI | ~€48 | €40 | |
| Redis | €20 | €34 | |
| ACR + web test + storage | ~€16 | €15 | |
| DocIntel / OpenAI (variable) | €53 / €40 | €64 / €43 | |

- The step on **2026-08-18** is all six workers going `min-replicas 1` at 4 vCPU / 8 GiB (KEDA scale-in workaround, `docs/HANDOVER-2026-08-18.md`). Billed almost entirely at the idle rate: ~€80/worker/month, not the $300 worst case in the handover.
- **2026-10-05: test workers set back to `min-replicas 0`** → −€240/mo. Fixed 3C floor now ≈ **€335/mo** (prod workers €244 + APIs/UI €40 + Redis €34 + misc €15).
- **Target floor once the KEDA fix lands and prod workers scale to zero:** ≈ €90–100/mo + active compute (~€0.02/doc). §1 uses €120 as a rounded, slightly conservative figure.
- Not 3C: `ca-garagenhub` + `-ui` (~€20/mo) run in the same RG/environment — exclude from 3C costing.
- DocIntel resource `document-intelligence-2510` lives in RG `company_finder`; Sep billed ~1.1K more pages than the metric → may be shared with another consumer.

## 5. Cost levers

In order of impact at current volume:

1. **Prod workers back to scale-to-zero** (needs the KEDA fix: orphaned-job sweep + understanding the scale-from-zero stall) — ~€240/mo.
2. **Fail >50-page documents before OCR**, or fix the analyze 50-image limit — ~10% of BPS OCR spend and the failures 3C reported.
3. **DocIntel Layout → Read**, if Read's table output is good enough — BPS ~−€0.12/doc. Needs a quality A/B.
4. **Pin a cheaper Mistral OCR model** (OCR 3 at $2/1K) if quality holds — ~−€0.002/page.
5. **gpt-5.6-terra** — ~10% cheaper than 5.4 at equal quality (eval 2026-07-27). Set extraction `detail: "high"` explicitly first: on 5.6, `auto` = `original` (no resize, up to ~36K tokens per image, rejected above 30K patches).

## 6. How to refresh these numbers

- Cost: Cost Management Query API (`ActualCost`, group by `ResourceId` + `Meter`), scope RGs `rg-3c-invoice`, `3c_information_extraction` + the DocIntel resource. Max 2 groupings per query; expect 429s (wait ~65 s).
- Tokens: `az monitor metrics list` on `3cinfoextraction`, metrics `InputTokens OutputTokens`, one month per query (a 66-day window silently drops data).
- DocIntel pages: metric `ProcessedPages` on `document-intelligence-2510`.
- Per-job tokens: worker logs in Log Analytics (`ContainerAppConsoleLogs_CL`, events `analyze_llm_call`, `llm_call`); only 30 days of retention, and the lines carry no `file_id` (match by replica + time order).
