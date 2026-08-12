# Fahrzeugausweis Extraction Service — Design

**Date**: 2026-07-06
**Author**: Bernhard Langwallner
**Status**: Approved (pending review of this spec)

## Context

3C's GaragenHub is being revamped and opened to additional Swiss insurers. In a
claim (Schaden), a garage needs both the keeper's (Halter) and the vehicle's
data. For the policyholder this is retrievable via a Kontrollschild (plate)
lookup against the insurer's book of business. For the **damaged third party
(Geschädigter)** it is not — the insurer usually doesn't know them. So GaragenHub
wants to let garages **upload a photo of the third party's Fahrzeugausweis (Swiss
vehicle registration document)** and have the required fields extracted and used
to prefill the claim form.

A colleague already prototyped this with Hypatos and it worked well even on
poor-quality photos. Now that the extraction competence is in-house, we build it
ourselves.

This is the **fourth product** conceptually, but architecturally it is different
from the three existing ones (vetcostcheck / bps / sanierer):

| | Existing 3 products | Fahrzeugausweis |
|---|---|---|
| Input | Multi-invoice PDFs | Single photographed card (JPEG/PNG/HEIC/PDF) |
| Structure | Split → per-subdocument → line items | One document, flat fixed field set |
| Processing | OCR (dual engine) → analyze/split → extract | One vision call, no OCR, no split |
| Delivery | Async: upload → RQ job → poll | **Synchronous**: user waits for the response |
| State | Redis + Blob storage + worker | Stateless, no persistence |

Because it fits the existing `ProductConfig` contract poorly (nothing to split,
no line items, sync not async) and because it is latency-critical, it is built as
a **separate lightweight repository/service**, not a fourth product in the
monorepo. It reuses the deployment *patterns* (Azure Container Apps, ACR,
deploy script style) but not the pipeline code.

## Feasibility & Latency (measured)

Both of the colleague's real phone photos were run through the intended approach
— a single vision call to `gpt-5.4`, image downscaled to 1600px, `detail: high`,
`temperature=0`, `seed=42`, no OCR, no splitting:

- **Model wall-time**: 4.3–6.3s, mean ~5s (4 runs over the two images).
- **End-to-end sync estimate**: comfortably under 10s (~5s compute + upload/network).
- **Cost**: ~$0.008 per document (~1,070 input + ~350 output tokens at the
  tracked gpt-5.4 GlobalStandard rate).
- **Accuracy**: field-perfect on both cards, identical across repeated runs
  (deterministic with temp 0 / seed). This includes **Kontrollschildfarbe**
  ("weiss", top-right) — the field the colleague thought was missing.

Findings that shaped the design:
- **`detail: high` is required for accuracy.** At `low` detail the VIN was
  misread; at `high` detail plate and VIN were exact. High detail adds no
  meaningful latency for a single image.
- **Downscaling to ~1600px is a free win**: same accuracy as full-res, slightly
  faster, ~5× smaller payload. GaragenHub should also downscale client-side to
  cut upload time.

**Conclusion**: a synchronous API is viable. A garage user waits ~5–8 seconds
for a fully populated form.

## Decision

Build a **new, standalone, stateless FastAPI service** (repo `garagenhub-extractor`,
sibling to `information_extraction`) exposing one synchronous extraction endpoint. Process the image in-process (normalize → single vision
call → validate) and return typed JSON. No worker, no Redis, no storage backend.
Deploy as a single always-warm Azure Container App.

Rejected alternatives:
- *Fourth product in the existing monorepo* — would force a new sync/no-split/
  single-image code path into `core/` that serves none of the other three
  products and risks complicating them.
- *Reuse the async job/poll model* — overkill for one small image and worse UX
  for a user who is actively waiting.
- *Shared `core/` as an imported library* — couples the two repos for the sake of
  ~50 lines of genuinely shared code (Azure client + preprocessing).

## Architecture

Stateless request/response. No background jobs.

```
POST /extract   (X-Api-Key header)
    multipart file upload (one image)
      → preprocessing: format detect, HEIC→JPEG, PDF→image(first page),
        downscale to 1600px, base64
      → extractor: build prompt, one gpt-5.4 vision call (detail:high,
        temp 0, seed, tenacity 3×), parse JSON
      → validation: deterministic per-field checks → warnings + validity
      → typed JSON response

GET  /healthz   (no auth)
```

**Privacy by default**: the uploaded image is held in memory only and never
persisted. Logs contain metadata (timings, token counts, per-field validity
flags) but no personal data and no image bytes.

**Always warm**: because latency matters, the Container App runs with
`min-replicas 1` (no scale-to-zero cold start), unlike the invoice workers.

## Components

Each is an isolated, independently testable unit.

| Module | Responsibility | Depends on |
|---|---|---|
| `api/main.py` | FastAPI app; `/extract` + `/healthz`; API-key auth dependency | config |
| `preprocessing.py` | bytes → normalized image: detect format, HEIC→JPEG, PDF→image (first page), downscale to 1600px, base64-encode | Pillow, pillow-heif, pypdfium2 |
| `extractor.py` | build content blocks, call Azure OpenAI vision (`detail:high`, temp 0, seed, tenacity 3× retry), parse JSON out of the response | openai, prompt, config |
| `prompt.py` | the German extraction prompt + the authoritative field list | — |
| `validation.py` | deterministic per-field checks (VIN length/charset, plate pattern, date parse, numeric coercion) → validity flags + warnings list | — |
| `models.py` | Pydantic request/response models (drives typed OpenAPI docs) | — |
| `config.py` | env: API key, Azure creds, model name, max upload size, request timeout | — |

## Output Contract

Shaped so GaragenHub can prefill the claim form field-for-field with no
post-processing. The AXA form splits fields the Ausweis combines, so we split at
extraction time:

- keeper name "Körner Christian" → `vorname` + `nachname`
- "Marke und Typ" "VW Golf" → `marke` + `modell`
- "Schwerzistrasse 20a" → `strasse` + `hausnummer`

```jsonc
{
  "halter": {
    "vorname": "Christian",
    "nachname": "Körner",
    "strasse": "Schwerzistrasse",
    "hausnummer": "20a",
    "plz": "8863",
    "ort": "Buttikon SZ",
    "versicherung": "AXA Versicherungen",
    "geburtsdatum": "08.05.1978"
  },
  "fahrzeug": {
    "kontrollschild": "SZ 41719",
    "kontrollschildfarbe": "weiss",
    "fahrzeugart": "Personenwagen",
    "marke": "VW",
    "modell": "Golf",
    "vin": "WVW ZZZ CDZ NW1 321 44",
    "karosserie": "Limousine",
    "farbe": "schwarz met.",
    "stammnummer": "162.336.502",
    "typengenehmigung": "X",
    "hubraum_cm3": 1984,
    "leistung_kw": 235.0,
    "leergewicht_kg": 1630,
    "gesamtgewicht_kg": 2030,
    "erste_inverkehrsetzung": "07.02.2022",
    "emissionscode": "B6d",
    "plaetze": 5
  },
  "interne_hinweise": "178 HALTERWECHSEL VERBOTEN …",
  "warnings": [
    { "field": "fahrzeug.stammnummer", "reason": "format_invalid" }
  ],
  "field_confidence": {
    "fahrzeug.stammnummer": "low",
    "halter.vorname": "medium"
  },
  "meta": { "model": "gpt-5.4", "processing_ms": 5000, "image_pages": 1 }
}
```

Contract rules:
- **Every value is always present**, `null` if unreadable — so the garage can
  prefill and correct rather than get a partial object.
- **`warnings`** = deterministic validation failures only (trustworthy signals:
  VIN length/charset, plate pattern, date parse, numeric coercion). This is the
  primary "please check these" cue for the garage user, matching the AXA UI's
  "Daten ausgelesen – bitte prüfen und bei Bedarf korrigieren".
- **`field_confidence`** = the model's coarse self-assessment (`high`/`medium`/
  `low`). A soft, weakly-calibrated signal — supplementary to `warnings`, not a
  substitute.
- Numeric fields (`hubraum_cm3`, `leistung_kw`, weights, `plaetze`) are coerced
  to numbers; the on-card leading-asterisk padding (e.g. `**1630`) is stripped.

### Field ↔ AXA form mapping

The output covers every field on the AXA "Geschädigte Partei" form:

| AXA form field | Response path |
|---|---|
| Vorname / Nachname | `halter.vorname` / `halter.nachname` |
| Kontrollschild | `fahrzeug.kontrollschild` |
| Kontrollschildfarbe | `fahrzeug.kontrollschildfarbe` |
| Chassisnummer (VIN) | `fahrzeug.vin` |
| Marke / Modell | `fahrzeug.marke` / `fahrzeug.modell` |
| Fahrzeugart | `fahrzeug.fahrzeugart` |
| Fahrzeugfarbe | `fahrzeug.farbe` |
| 1. Inverkehrsetzung | `fahrzeug.erste_inverkehrsetzung` |
| Stammnummer | `fahrzeug.stammnummer` |
| Strasse / Nr. | `halter.strasse` / `halter.hausnummer` |
| PLZ / Ort | `halter.plz` / `halter.ort` |

## Error Handling

- `400` — no file, unsupported format, corrupt/undecodable image, or oversized
  upload (over configured max).
- `401` — missing/invalid `X-Api-Key`.
- `422` / `502` — model returns unparseable JSON: one internal reparse attempt,
  then `502`.
- `502`/`503` — Azure OpenAI failure after tenacity retries (3 attempts,
  exponential backoff) exhausted.
- Server-side request timeout (~30s) so a hung upstream never holds a connection.
- All errors reported to Sentry with no PII (no image, no extracted values).

## Testing

- **Unit** (Azure call mocked for determinism):
  - `preprocessing`: each input format (JPEG/PNG/HEIC/PDF), downscale behavior,
    oversized/corrupt rejection.
  - `validation`: VIN, plate, date, and numeric edge cases (valid + invalid).
  - `prompt`: builder produces the expected field list.
- **Golden CLI**: `scripts/extract_local.py <image>` prints the JSON for manual
  spot-checks.
- **Integration** (opt-in, skipped in CI unless Azure creds present): run the two
  sample cards end-to-end and assert key fields (plate, VIN, marke, dates).

## Streamlit UI

A thin operator/demo UI, in the same repo under `ui/`, deployable separately. It
is a **pure HTTP client of the `/extract` API** — it never imports the extraction
code or talks to Azure directly. This keeps UI and API as two independently
routable, independently scalable services.

- **Flow**: password gate (optional) → file uploader (JPEG/PNG/HEIC/PDF) →
  POST to the API's `/extract` → render an editable, AXA-form-shaped review screen
  (Halter + Fahrzeug sections) beside a preview of the uploaded image.
- **Review affordances**: every field is an editable input pre-filled with the
  extracted value; fields in `warnings` are flagged (⚠️) and fields with
  medium/low `field_confidence` are badged, so the operator knows what to check —
  mirroring the AXA UI's "bitte prüfen und bei Bedarf korrigieren". Meta (model,
  processing_ms) and a raw-JSON expander are shown for transparency.
- **Auth**: the UI holds `EXTRACTOR_API_KEY` server-side (Streamlit runs
  server-side; the key is never sent to the browser). An optional shared-password
  gate (`UI_PASSWORD`) protects the UI itself — disabled when unset (local dev),
  enabled in prod.
- **Config (env)**: `EXTRACTOR_API_URL` (e.g. `http://localhost:8080` locally,
  the API's DNS route in prod), `EXTRACTOR_API_KEY`, optional `UI_PASSWORD`.
- **Testability**: the HTTP client (`ui/api_client.py`) and the password check
  (`ui/auth.py`) are unit-tested with `requests` mocked; the Streamlit rendering
  layer (`ui/app.py`) is kept thin and verified manually via `streamlit run`.

## Deployment

- New repository `garagenhub-extractor`. Reuse ACR `cr3cinvoice` and Container
  Apps environment `cae-3c-invoice`. Unique image tags per deploy (`latest`
  silently skips new revisions).
- **Two apps, two Dockerfiles, two DNS routes**:
  - `ca-garagenhub` — the API. Root `Dockerfile`. **API only** (no worker),
    `min-replicas 1` (always warm for sync latency), modest CPU/memory.
  - `ca-garagenhub-ui` — the Streamlit UI. `ui/Dockerfile`. Points at the API via
    `EXTRACTOR_API_URL`. Deployed for prod (demo → prod path includes the UI).
- **API env**: `AZURE_OPENAI_KEY`, `AZURE_ENDPOINT`, `AZURE_OPENAI_API_VERSION`,
  `OPENAI_VISION_MODEL` (default `gpt-5.4`), `EXTRACTOR_API_KEY`,
  `MAX_UPLOAD_MB`, `REQUEST_TIMEOUT_S`, optional `SENTRY_DSN`.
- **UI env**: `EXTRACTOR_API_URL`, `EXTRACTOR_API_KEY`, optional `UI_PASSWORD`.

## Open Questions

- **Data residency** (owner: Bernhard / legal): Swiss insurance personal data
  (Halterdaten) is processed by Azure OpenAI. Default assumption is that EU
  processing (Germany West Central, the existing deployment) is acceptable. If
  CH-only residency is contractually required, switch to Azure OpenAI in
  Switzerland North — this needs a capacity/availability check before committing.
  Proceed on the EU assumption; confirm before go-live.

## Scope Boundaries (YAGNI for v1)

- **Single-tenant** at launch: one API key for GaragenHub. Schema and logging are
  designed so per-insurer keys / usage attribution can be added later without
  rework, but full multi-tenancy is deferred until a second insurer is real.
- **Single image per request**: the opened Ausweis fits in one photo (as in both
  samples). Multi-image (separate front/back) is out of scope for v1.
- **No auto-retry on low confidence**: a single vision call; the human corrects
  flagged fields. Re-asking the model would add latency for marginal gain.
- **No persistence / no audit store** of images or extractions in v1.
