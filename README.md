# 3C Extraction Platform

Document-extraction platform serving multiple insurance products. Each product (currently: `vetcostcheck`; planned: `bps`, `sanierer`) is implemented as a separate Container App pair on top of a shared `core/` library.

- `core/` — shared framework: API, RQ worker, OCR, storage, LLM processors, pipeline
- `products/<name>/` — per-product `ProductConfig`, prompts, schemas
- `docs/superpowers/specs/` — design specs
- `docs/superpowers/plans/` — implementation plans

See `CLAUDE.md` for development setup and `docs/superpowers/specs/2026-05-07-multi-product-extraction-design.md` for the platform design.
