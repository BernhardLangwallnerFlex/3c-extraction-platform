Here's the full breakdown:

  Cost per document: VCC_Viele_Dokumente.pdf (6 pages, 4 invoices)

  ┌───────────────────────────────────────────────┬────────┬────────────┐
  │                     Step                      │  Cost  │ % of total │
  ├───────────────────────────────────────────────┼────────┼────────────┤
  │ OCR (Mistral $0.006 + Azure $0.009)           │ $0.015 │    7.9%    │
  ├───────────────────────────────────────────────┼────────┼────────────┤
  │ Analysis (1 LLM call, 9.6K in / 626 out)      │ $0.033 │   17.6%    │
  ├───────────────────────────────────────────────┼────────┼────────────┤
  │ Extraction (4 LLM calls, 17.7K in / 6.5K out) │ $0.141 │   74.5%    │
  ├───────────────────────────────────────────────┼────────┼────────────┤
  │ Total                                         │ $0.19  │            │
  ├───────────────────────────────────────────────┼────────┼────────────┤
  │ Per page                                      │ $0.032 │            │
  └───────────────────────────────────────────────┴────────┴────────────┘

  Key insights

  Extraction dominates at 74.5% of cost — driven by gpt-5.4's $15/1M output tokens. Invoice 3 alone (22 items, 4K output tokens) costs $0.077.

  OCR is now cheap — just 7.9% of total cost. The switch from LandingAI ($0.18 for 6 pages) to dual Mistral+Azure ($0.015) was a 12x reduction.

  Analysis is modest — the page images (150 DPI, detail=low) are token-efficient.

  vs. old pipeline (LandingAI + gpt-4o)

  ┌────────────┬────────┬────────┬──────────────────────────────────┐
  │            │  Old   │  New   │              Change              │
  ├────────────┼────────┼────────┼──────────────────────────────────┤
  │ OCR        │ $0.180 │ $0.015 │ -92%                             │
  ├────────────┼────────┼────────┼──────────────────────────────────┤
  │ Analysis   │ $0.030 │ $0.033 │ +10% (images added)              │
  ├────────────┼────────┼────────┼──────────────────────────────────┤
  │ Extraction │ $0.109 │ $0.141 │ +30% (gpt-5.4 output costs more) │
  ├────────────┼────────┼────────┼──────────────────────────────────┤
  │ Total      │ $0.319 │ $0.190 │ -41%                             │
  └────────────┴────────┴────────┴──────────────────────────────────┘

  The net savings are 41% despite gpt-5.4's higher output token price, because the OCR cost collapsed. For a typical 1-2 page single-invoice doc the cost would be roughly $0.03-0.06.

✻ Cooked for 4m 0s

❯ nice, just out of interest: what would be the cost with gpt-5.4-mini? (we actually never tried it in production, I guess)
  ⎿  UserPromptSubmit hook error

⏺ Bash(cd "/Users/bernhardlangwallner/Documents/05 Coding/3C/information_extraction" && .venv/bin/python -c "
      # Same token counts from VCC_Viele_Dokumente.pdf test run…)
  ⎿  gpt-5.4:
       OCR:        $0.0150
       Analysis:   $0.0334
     … +10 lines (ctrl+o to expand)

⏺ ┌────────────┬─────────┬──────────────┬───────────┐
  │            │ gpt-5.4 │ gpt-5.4-mini │   Diff    │
  ├────────────┼─────────┼──────────────┼───────────┤
  │ OCR        │ $0.015  │ $0.015       │ same      │
  ├────────────┼─────────┼──────────────┼───────────┤
  │ Analysis   │ $0.033  │ $0.010       │ 3.3x less │
  ├────────────┼─────────┼──────────────┼───────────┤
  │ Extraction │ $0.141  │ $0.042       │ 3.3x less │
  ├────────────┼─────────┼──────────────┼───────────┤
  │ Total      │ $0.190  │ $0.067       │ 2.8x less │
  ├────────────┼─────────┼──────────────┼───────────┤
  │ Per page   │ $0.032  │ $0.011       │           │
  └────────────┴─────────┴──────────────┴───────────┘

  So mini would be $0.067 per document vs $0.190 — roughly 1 cent per page instead of 3 cents. The tradeoff is the consistency issue we saw (9 vs 20 items). But no, we never actually tested mini with the correct env vars in production —
  every "production" test was secretly running gpt-4o due to the override. So we don't truly know how mini performs in the current setup (dual OCR + improved prompt + item count hint). Could be worth revisiting if cost becomes a concern
  at scale.