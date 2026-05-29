# Sanierer — Notes & Open Items for the Product Owner / Domain Experts

From the first-pass Sanierer extraction (v1) on the 7 sample PDFs in
`bps_sanierer_input/Sanierer_Input/`. v1 was deployed for expert testing.
Extraction quality was high: all 7 reconcile on both `net+tax ≈ gross` and
`Σ items ≈ net`, and both `position` and `lvPosition` were captured on 100% of
line items.

Status: **awaiting product-owner input.** Date raised: 2026-05-29.

## 1. Scope: header data intentionally omitted
Per the Erfassungsmaske, only line-item positions are extracted (Belegart,
number, date, items, totals). Sanierer firm / Schadenort / Schadennummer /
recipient are NOT extracted — they come from the Auftrag. **Confirm** experts
don't need any header field from the Beleg itself (additive if so; the BPS
schema already models these fields).

## 2. Belegart values
v1 `type` enum is `invoice` (Rechnung) / `quote` (Angebot). Samples also include
Verkaufsrechnung (classified as `invoice`). **Confirm** no other Belegarten are
needed.

## Minor prompt-refinement items (non-blocking, for a future pass)
- **Advisory item-count warning noise:** the analyze step's predicted position
  count sometimes under-counts vs what extraction actually finds (e.g. "13 vs
  17"); the model emits a `warning`. The extraction itself is complete (Σitems ≈
  net passes). Could tighten the analyze "Frage 4" counting or drop the hint to
  reduce warning noise.
- **OCR position artifacts:** in `AR26076770.pdf`, hierarchical position dots were
  OCR'd as spaces (`2 1 1`); captured faithfully, `lvPosition` correct. Could add
  a normalization rule if experts want canonical dotted position numbers.
