# BPS Product Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Onboard `bps` (Belegprüfung Sach — property/contents-insurance receipt verification) as a new product on the multi-product platform, from a domain-neutral core refactor through to deployed Container Apps, leaving the custom-domain cutover for a later batch.

**Architecture:** First harmonize the vet-specific `animal_information` seam into a generic `subdocument_context` so `core/` names no domain entities (regression-guarded). Then add `products/bps/` mirroring the proven `products/vetcostcheck/` structure (schema + extraction prompt + analyze/split override + `ProductConfig`). The shared `core.pipeline.Pipeline`, `deploy.sh`, `scripts/provision_product.sh`, and the parameterized `Dockerfile` are reused unchanged.

**Tech Stack:** Python 3.11, FastAPI, RQ, Redis, Azure Container Apps, Azure OpenAI, Azure Document Intelligence, Mistral OCR, Azure Blob Storage. Domain language: German.

**Spec:** `docs/superpowers/specs/2026-05-29-bps-extraction-design.md`

---

## File Structure

**Refactor (Task 1) — modify:**
- `core/pipeline.py` — generalize the per-subdocument context derivation + builder/processor kwargs.
- `core/processors/azure_processor.py`, `core/processors/gpt_processor.py` — rename the `animal_information` param.
- `products/vetcostcheck/extract_prompt.py` — rename the `animal_information` param.
- `products/vetcostcheck/analyze_overrides.py` — rename analyze output keys.
- `tests/products/vetcostcheck/test_smoke.py` — update the kwarg used in the test.

**New product (Tasks 2–7) — create:**
- `products/bps/__init__.py`
- `products/bps/extract_schema.json` — output JSON Schema (documentation/validation target).
- `products/bps/extract_prompt.py` — `build_extract_prompt(...)` German extraction prompt.
- `products/bps/analyze_overrides.py` — `build_analyze_prompt(...)` + `ANALYZE_OUTPUT_SCHEMA`.
- `products/bps/product.py` — exports `CONFIG: ProductConfig`.
- `tests/products/bps/__init__.py`, `tests/products/bps/test_smoke.py`

**Tooling (Task 8) — create:**
- `scripts/extract_local.py` — run the full pipeline on one local PDF for any product, print JSON.

**Deployment (Task 9) — no file changes:** `deploy.sh` already lists `bps` in its `PRODUCTS` array; `scripts/provision_product.sh` and `Dockerfile` are product-generic.

---

## Task 1: Harmonize `animal_information` → `subdocument_context` (regression-guarded)

Make `core/` domain-neutral. This is a behavior-preserving rename: vet still feeds its per-invoice animals into extraction, just under a generic name. The vetcostcheck regression check is the safety net — vet's final output must not change.

**Files:**
- Modify: `core/pipeline.py`
- Modify: `core/processors/azure_processor.py:59,80`
- Modify: `core/processors/gpt_processor.py:17,24`
- Modify: `products/vetcostcheck/extract_prompt.py:5-23`
- Modify: `products/vetcostcheck/analyze_overrides.py`
- Modify: `tests/products/vetcostcheck/test_smoke.py:22`

- [ ] **Step 1: Capture the pre-refactor vet regression baseline**

The regression references are not committed (customer-shaped data; captured locally per checkpoint). Capture them against current `main` before touching anything.

```bash
docker compose up redis -d
# Ensure tests/regression/inputs/ has 3–5 representative vet PDFs (see the prior plan, Task 3).
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 PRODUCT_NAME=vetcostcheck \
  python scripts/regression_check.py --capture
ls tests/regression/references/
```

Expected: one JSON per input PDF. This is the pre-refactor snapshot — do not edit these files during the task.

- [ ] **Step 2: Generalize the context derivation in `core/pipeline.py`**

Replace the vet-specific block at `core/pipeline.py:285-294` (currently reading `invoice_animals` / `animals`):

```python
        # Use per-invoice animals if available, fall back to global list
        invoice_animals = self.analysis_dict.get("invoice_animals", {})
        animal_info = None
        if invoice_animals:
            if invoice_key and invoice_key in invoice_animals:
                animal_info = invoice_animals[invoice_key]
            elif str(subdoc.document_number) in invoice_animals:
                animal_info = invoice_animals[str(subdoc.document_number)]
        if animal_info is None:
            animal_info = self.analysis_dict.get("animals")
```

with this domain-neutral version:

```python
        # Per-subdocument context produced by the product's analyze override
        # (e.g. vetcostcheck fills it with the animals on each sub-invoice; BPS
        # leaves it absent). Core is domain-agnostic — it only slices the map by
        # document key and falls back to a global blob.
        subdocument_context_map = self.analysis_dict.get("subdocument_context", {})
        subdocument_context = None
        if subdocument_context_map:
            if invoice_key and invoice_key in subdocument_context_map:
                subdocument_context = subdocument_context_map[invoice_key]
            elif str(subdoc.document_number) in subdocument_context_map:
                subdocument_context = subdocument_context_map[str(subdoc.document_number)]
        if subdocument_context is None:
            subdocument_context = self.analysis_dict.get("subdocument_context_global")
```

- [ ] **Step 3: Rename the kwargs in the `return processor.extract(...)` call**

In `core/pipeline.py` (currently lines 304-315), change the two `animal_information=animal_info` occurrences to use the new variable name:

```python
        return processor.extract(
            str(local_image),
            use_ocr=True,
            use_vision=True,
            markdown_text=subdoc.markdown,
            prompt=self.product_config.extract_prompt_builder(
                ocr_text=ocr_text,
                subdocument_context=subdocument_context,
                expected_items=expected_items,
            ),
            subdocument_context=subdocument_context,
        )
```

- [ ] **Step 4: Rename the param in both processors**

In `core/processors/azure_processor.py:59`, change the signature:

```python
    def extract(self, img_file_path: str, use_ocr=True, use_vision=True, markdown_text="", prompt="", subdocument_context={}) -> str:
```

And the fallback call at `core/processors/azure_processor.py:80` (keep `build_prompt_from_config`'s own legacy param name; just pass the renamed variable):

```python
        if prompt == "":
            prompt = build_prompt_from_config("configs/extraction_config.json", use_ocr=use_ocr, use_vision=use_vision, ocr_text=markdown_text, animal_information=subdocument_context)
```

Update the docstring line `core/processors/azure_processor.py:69` from `animal_information: Additional context information` to `subdocument_context: Optional per-subdocument context (product-specific)`.

Apply the identical changes to `core/processors/gpt_processor.py:17` (signature) and `core/processors/gpt_processor.py:24` (fallback call):

```python
    def extract(self, img_file_path: str, use_ocr=True, use_vision=True, markdown_text="", prompt="", subdocument_context={}) -> str:
```
```python
            prompt = build_prompt_from_config("configs/extraction_config.json", use_ocr=use_ocr, use_vision=use_vision, ocr_text=markdown_text, animal_information=subdocument_context)
```

> Note: `build_prompt_from_config` (in `core/prompt_building/prompt_building.py`) is the legacy vet-config path and is never reached in production (the pipeline always passes a built `prompt=`). Leave its internal `animal_information` param name as-is — renaming dead code is out of scope.

- [ ] **Step 5: Rename the param in vetcostcheck's extract prompt**

In `products/vetcostcheck/extract_prompt.py`, change the signature (lines 5-10) so `animal_information` becomes `subdocument_context`, keeping the internal logic identical:

```python
def build_extract_prompt(
    *,
    ocr_text: str = "",
    subdocument_context: list[dict] | None = None,
    expected_items: int | None = None,
) -> str:
    """Build the extraction prompt for a single vet sub-invoice.

    `subdocument_context` carries the per-invoice animals detected during the
    analyze stage (formerly `animal_information`). The prompt body is copied
    verbatim from get_full_prompt(...) — do not paraphrase.
    """
    if subdocument_context:
        animals_section = "\n".join([f"{animal['name']} (Tierart: {animal['species']}, Rasse: {animal['breed']})"
                                    if animal['breed'] != ""
                                    else f"{animal['name']} (Tierart: {animal['species']})"
                                    for animal in subdocument_context])
        animals_section = f"Die folgenden Tiere werden in der Rechnung oder Quittung erwähnt: {animals_section}. Diese Information ist wichtig für die Extrahierung der Leistungen auf der Rechnung oder Quittung."
    else:
        animals_section = ""
```

(Everything below line 23 — `items_hint` onward and the returned prompt string — is unchanged.)

- [ ] **Step 6: Rename the analyze output keys in vetcostcheck's analyze override**

In `products/vetcostcheck/analyze_overrides.py`, the analyze prompt instructs the LLM to emit `animals` (Frage 5) and `invoice_animals` (Frage 6). Rename those output keys to the generic names so core's generalized reader (Step 2) picks them up. The animal-dict *content* stays identical.

Replace the Frage 5 line (currently line 32):

```python
    "Frage 5: Welche Tiere werden genannt und welcher Spezies (z.B. Hund, Katze, etc.) bzw. Rasse (z.B. Labrador, Bulldog, etc.) gehören sie an? Dazu noch Informationen wie Geburtsdatum, Geschlecht, Chip-ID, Diagnose, etc. so weit vorhanden. Output als Liste von Dictionaries: 'subdocument_context_global': [{{'name': str, 'species': str, 'breed': str, 'birthDate': str, 'gender': str, 'chipId': str, 'diagnosis': str}}, {{'name': str,...}},...] und so weiter, falls es mehrere Tiere gibt.\n"
```

Replace the Frage 6 line (currently line 33):

```python
    "Frage 6: Welche Tiere gehören zu welcher Rechnung? Output: 'subdocument_context': {{<invoice_number>: [<liste der Tiere als Dictionaries wie in Frage 5>], ...}}. Wenn eine Rechnung kein Tier enthält, soll die Liste leer sein. Weise Tiere NUR den Rechnungen zu, auf denen sie tatsächlich erwähnt werden.\n"
```

Then rename the two corresponding properties in `ANALYZE_OUTPUT_SCHEMA` (currently the `"animals"` key at line 68 and the `"invoice_animals"` key at line 83):

```python
        "subdocument_context_global": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "species": {"type": ["string", "null"]},
                    "breed": {"type": ["string", "null"]},
                    "birthDate": {"type": ["string", "null"]},
                    "gender": {"type": ["string", "null"]},
                    "chipId": {"type": ["string", "null"]},
                    "diagnosis": {"type": ["string", "null"]},
                },
            },
        },
        "subdocument_context": {
            "type": "object",
            "description": "Map of <invoice_number> (str) -> list of animal dicts (per-subdocument context).",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
```

Also update the module docstring (lines 3-6) and the `ANALYZE_OUTPUT_SCHEMA` `"description"` (line 50) to refer to `subdocument_context` rather than `invoice_animals`.

- [ ] **Step 7: Update the vetcostcheck smoke test kwarg**

In `tests/products/vetcostcheck/test_smoke.py:22`, change:

```python
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", subdocument_context={})
```

- [ ] **Step 8: Run the smoke tests**

Run: `python -m pytest tests/products/vetcostcheck/test_smoke.py tests/core/test_product.py -v`
Expected: all PASS (the rename didn't break loading or prompt building).

- [ ] **Step 9: Smoke-import core to catch missed references**

```bash
python -c "
from core.pipeline import Pipeline
from core.processors.azure_processor import AzureInvoiceProcessor
from core.processors.gpt_processor import GPTInvoiceProcessor
print('imports ok')
"
grep -rn "animal_information\|invoice_animals" core/ products/vetcostcheck/ tests/products/vetcostcheck/ \
  | grep -v "build_prompt_from_config" | grep -v "\.pyc"
```

Expected: `imports ok`, and the grep returns nothing except (optionally) the two intentional `animal_information=subdocument_context` calls into the legacy `build_prompt_from_config`.

- [ ] **Step 10: Run the regression check (the real gate)**

```bash
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 PRODUCT_NAME=vetcostcheck \
  python scripts/regression_check.py
```

Expected: `[PASS]` for every PDF. If `[FAIL]`, the animal data is no longer reaching extraction — re-check that vet's analyze override emits `subdocument_context` (Step 6) and core reads it (Step 2). Re-run once if a known LLM flake appears (see prior plan's Task 8 deviation note); a persistent structural/numeric diff is a real regression.

- [ ] **Step 11: Commit**

```bash
git add core/pipeline.py core/processors/azure_processor.py core/processors/gpt_processor.py \
        products/vetcostcheck/extract_prompt.py products/vetcostcheck/analyze_overrides.py \
        tests/products/vetcostcheck/test_smoke.py
git commit -m "Harmonize animal_information -> subdocument_context (core now domain-neutral)"
```

---

## Task 2: Create the `products/bps/` package skeleton

**Files:**
- Create: `products/bps/__init__.py`

- [ ] **Step 1: Create the package**

```bash
touch products/bps/__init__.py
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "import products.bps; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add products/bps/__init__.py
git commit -m "Add empty products/bps/ package"
```

---

## Task 3: Add the BPS output schema

**Files:**
- Create: `products/bps/extract_schema.json`

- [ ] **Step 1: Write the schema**

Create `products/bps/extract_schema.json` with the full per-subdocument schema from the spec (§4.2/§4.3):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "BPS extraction output",
  "description": "Structured Beleg data extracted from a single BPS sub-document (Belegprüfung Sach). Documentation + future validation target; not currently enforced against the LLM output. Wrapped by the pipeline in {number_of_subdocuments, subdocuments[]}.",
  "type": "object",
  "properties": {
    "type": {"type": ["string", "null"], "enum": ["invoice", "quote", null], "description": "Belegart: invoice=Rechnung, quote=Angebot"},
    "currency": {"type": ["string", "null"], "description": "ISO 4217 code, e.g. EUR"},
    "number": {"type": ["string", "null"], "description": "Belegnummer (Rechnungs-/Angebotsnummer)"},
    "issuedAt": {"type": ["string", "null"], "description": "Belegdatum, YYYY-MM-DD"},
    "sender": {
      "type": "object",
      "description": "Belegersteller / Handwerker — the firm that issued the Beleg",
      "properties": {
        "companyName": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"]},
        "postcode": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]},
        "contactPhone": {"type": ["string", "null"]},
        "contactMail": {"type": ["string", "null"]},
        "vatId": {"type": ["string", "null"], "description": "USt-IdNr"}
      }
    },
    "serviceProvider": {
      "type": "object",
      "description": "Dienstleister — service provider (often identical to sender)",
      "properties": {
        "companyName": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"]},
        "postcode": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]},
        "contactPhone": {"type": ["string", "null"]},
        "contactMail": {"type": ["string", "null"]},
        "vatId": {"type": ["string", "null"]}
      }
    },
    "payment": {
      "type": "object",
      "properties": {
        "iban": {"type": ["string", "null"]},
        "bic": {"type": ["string", "null"]},
        "bankName": {"type": ["string", "null"]},
        "dueDate": {"type": ["string", "null"], "description": "YYYY-MM-DD"}
      }
    },
    "recipient": {
      "type": "object",
      "description": "Rechnungsanschrift — addressee of the Beleg",
      "properties": {
        "companyName": {"type": ["string", "null"]},
        "contactFirstname": {"type": ["string", "null"]},
        "contactName": {"type": ["string", "null"]},
        "street": {"type": ["string", "null"]},
        "postcode": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]},
        "contactPhone": {"type": ["string", "null"]},
        "contactMail": {"type": ["string", "null"]}
      }
    },
    "policyholder": {
      "type": "object",
      "description": "Versicherungsnehmer — policyholder",
      "properties": {
        "name": {"type": ["string", "null"]},
        "address": {"type": ["string", "null"]},
        "postcode": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]}
      }
    },
    "damageLocation": {
      "type": "object",
      "description": "Schadenort — place of damage (~95% = recipient address; may come from cover email / Betreff)",
      "properties": {
        "address": {"type": ["string", "null"]},
        "postcode": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]}
      }
    },
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "position": {"type": ["string", "null"], "description": "Positionsnummer"},
          "name": {"type": ["string", "null"], "description": "Beschreibung (full position text)"},
          "qty": {"type": ["number", "null"], "description": "Menge"},
          "unit": {"type": ["string", "null"], "description": "raw/canonical unit text, e.g. Stk, m²"},
          "unitCode": {"type": "integer", "minimum": 0, "maximum": 30, "description": "0–30 enum; default 0 (PIECE)"},
          "unitPriceNet": {"type": ["number", "null"], "description": "E-Preis"},
          "lineTotalNet": {"type": ["number", "null"], "description": "G-Preis"},
          "taxRate": {"type": ["number", "null"], "description": "MwSt % for this line (usually null)"},
          "discount": {"type": ["number", "null"], "description": "Rabatt/Skonto for this line (usually null)"},
          "source": {"type": "object", "properties": {"snippet": {"type": "string"}}}
        }
      }
    },
    "totals": {
      "type": "object",
      "properties": {
        "net": {"type": ["number", "null"]},
        "tax": {"type": "object", "properties": {"rate": {"type": ["number", "null"]}, "amount": {"type": ["number", "null"]}}},
        "gross": {"type": ["number", "null"]},
        "discount": {"type": ["number", "null"]}
      }
    },
    "warnings": {"type": "array", "items": {"type": "string"}}
  }
}
```

- [ ] **Step 2: Verify it parses**

Run: `python -c "import json; d=json.load(open('products/bps/extract_schema.json')); print(sorted(d['properties'].keys()))"`
Expected: a list including `damageLocation`, `items`, `policyholder`, `recipient`, `sender`, `serviceProvider`, `totals`, `type`.

- [ ] **Step 3: Commit**

```bash
git add products/bps/extract_schema.json
git commit -m "Add BPS extraction output schema"
```

---

## Task 4: Add the BPS extraction prompt (v1)

This is the first-draft German extraction prompt. It is complete and runnable; expect to refine wording during local iteration (Task 8). It mirrors vetcostcheck's prompt structure but targets the BPS schema, drops vet-only fields, adds the unit enum, and ignores `subdocument_context`.

**Files:**
- Create: `products/bps/extract_prompt.py`

- [ ] **Step 1: Write the prompt builder**

Create `products/bps/extract_prompt.py`:

```python
"""BPS (Belegprüfung Sach) extraction prompt (German).

Mirrors the vetcostcheck prompt structure but targets the BPS schema:
Handwerker-Belege (Rechnungen/Angebote) for property/contents insurance claims.
"""
from __future__ import annotations


def build_extract_prompt(
    *,
    ocr_text: str = "",
    subdocument_context: list[dict] | None = None,
    expected_items: int | None = None,
) -> str:
    """Build the extraction prompt for a single BPS sub-document.

    `subdocument_context` is accepted for signature compatibility with the
    pipeline but is unused for BPS (no per-subdocument context is produced).
    """
    if expected_items and expected_items > 0:
        items_hint = (
            f"WICHTIG: Dieser Beleg enthält voraussichtlich etwa {expected_items} Positionen. "
            f"Wenn du weniger als {expected_items} Positionen findest, überprüfe nochmals den "
            f"OCR-Text und das Bild — wahrscheinlich hast du Zeilen übersehen."
        )
    else:
        items_hint = ""

    return (
"Du bist ein Experte für die Prüfung von Handwerker-Belegen (Rechnungen und Angeboten) "
"im Bereich der Sachversicherung (Hausrat / Wohngebäude).\n"
"Deine Aufgabe ist es, aus dem untenstehenden Beleg strukturierte Informationen zu extrahieren "
"und sie ausschließlich als gültiges JSON-Objekt im definierten Schema zurückzugeben.\n"
"Erfinde keine Werte. Wenn ein Feld nicht sicher ermittelt werden kann, gib null zurück und "
"erkläre Unsicherheiten im Feld 'warnings'.\n"
f"{items_hint}\n"
"Der Beleg ist als Bild (visuelle Referenz) sowie als OCR-Text aus zwei unabhängigen OCR-Systemen "
"verfügbar. Der OCR-Text ist zwischen Doppel-Pipes (||) angegeben und enthält zwei mit "
"'OCR Source A' und 'OCR Source B' gekennzeichnete Abschnitte. Nutze beide OCR-Quellen, um Fehler "
"zu erkennen und zu korrigieren. Bei Widersprüchen zwischen den OCR-Quellen überprüfe mit dem Bild.\n\n"
"OCR-Text:\n"
f"||\n{ocr_text}\n||\n\n"
"Regeln für die Extraktion:\n"
"1. Keine Halluzinationen: Nur Werte extrahieren, die im OCR- oder Bildinhalt sichtbar oder eindeutig ableitbar sind.\n"
"2. Wenn ein Feld fehlt oder nicht eindeutig ist → null.\n"
"3. Strings: ohne führende/trailing Leerzeichen.\n"
"4. Geldbeträge: nur Ziffern und Punkt als Dezimaltrennzeichen, z. B. 1985.00. Tausenderpunkte entfernen.\n"
"5. Datumsformat: YYYY-MM-DD.\n"
"6. Währung: ISO-4217-Code (z. B. \"EUR\").\n"
"7. Belegart ('type'): 'invoice' für eine Rechnung, 'quote' für ein Angebot/Kostenvoranschlag. Wenn unklar → null.\n"
"8. 'sender' ist der Belegersteller bzw. Handwerker, der den Beleg ausgestellt hat (Firmenname, Anschrift, USt-IdNr in 'vatId'). Die IBAN/BIC des Belegerstellers gehören in 'payment'.\n"
"9. 'serviceProvider' ist der Dienstleister. Häufig identisch mit dem Belegersteller — wenn kein separater Dienstleister erkennbar ist, übernimm die Werte des 'sender'.\n"
"10. 'recipient' ist die Rechnungsanschrift (an wen der Beleg adressiert ist).\n"
"11. 'policyholder' ist der Versicherungsnehmer. Dieser steht oft NICHT in der Rechnungsanschrift, sondern im Betreff/Schadenbezug (z. B. 'Einbruchschaden <Name>, <Anschrift>') oder in einer beigefügten E-Mail. Wenn nicht ermittelbar → null-Felder.\n"
"12. 'damageLocation' ist der Schadenort. In ca. 95 % der Fälle entspricht er der Rechnungsanschrift ('recipient'). Wenn jedoch im Betreff oder in der E-Mail eine abweichende Schadenanschrift genannt wird, nutze diese und vermerke die Abweichung in 'warnings'.\n"
"13. Zeilen mit Summe, Zwischensumme, Nettosumme, MwSt, USt, Gesamt, Bruttosumme, Saldo → NICHT als items übernehmen; sie gehören in 'totals'.\n"
"14. Eine Position mit erkennbarer Beschreibung UND einem Preis → ein items-Eintrag. Erfasse pro Position: 'position' (Positionsnummer/laufende Nummer, falls vorhanden), 'name' (vollständiger Beschreibungstext, auch über mehrere Zeilen), 'qty' (Menge), 'unit', 'unitCode', 'unitPriceNet' (Einzelpreis/E-Preis), 'lineTotalNet' (Gesamtpreis der Zeile/G-Preis), 'taxRate' und 'discount' falls je Position angegeben (sonst null).\n"
"15. Einheit ('unit' = Rohtext wie 'Stk', 'm²', 'Std'; 'unitCode' = passender Code aus der folgenden Liste). Wenn keine Einheit angegeben ist oder keine passt, setze unit=null und unitCode=0 (Stück).\n"
"    0=Stk, 1=mm, 2=mm², 3=mm³, 4=cm, 5=cm², 6=cm³, 7=m, 8=m², 9=m³, 10=Woche, 11=Monat, 12=kg, 13=Std, 14=Tag, 15=km, 16=%, 17=l, 18=lm, 19=pauschal, 20=kWh, 21=Paar, 22=t, 23=AW, 24=Satz, 25=Stange, 26=g, 27=StWo, 28=Sonstige, 29=Kilowatt Peak, 30=Grad.\n"
"16. Quellreferenzen: gib einen kurzen Textausschnitt der extrahierten Zeile in source.snippet an.\n"
"17. Totals: normalisiere alle Zahlenwerte. Steuersatz/Steuerbetrag NUR extrahieren, wenn explizit angegeben; sonst tax.rate=null, tax.amount=null. 'discount' in totals = Rabatt/Skonto auf Belegebene.\n"
"18. Validierung: wenn totals.net, totals.tax.amount und totals.gross vorhanden sind, prüfe totals.net + totals.tax.amount ≈ totals.gross (Toleranz ±0.02) und vermerke Abweichungen in 'warnings'.\n"
"19. IBAN (DE) = 22 Zeichen; BIC = 8 oder 11 Zeichen, upper-case.\n"
"20. Beigefügte E-Mails oder Anschreiben sind KEINE eigenen Positionen. Nutze sie nur als Kontext für Versicherungsnehmer und Schadenort.\n"
"21. Alle Positionen in der Reihenfolge des Belegs extrahieren. Achte besonders darauf, ALLE Zeilen innerhalb von Tabellen zu erfassen — nicht zusammenfassen, nicht deduplizieren.\n\n"
"JSON-Ziel-Schema:\n"
"{\n"
"\"type\": \"invoice|quote|null\",\n"
"\"currency\": \"EUR|null\",\n"
"\"number\": \"string|null\",\n"
"\"issuedAt\": \"YYYY-MM-DD|null\",\n"
"\"sender\": {\n"
"  \"companyName\": \"string|null\", \"address\": \"string|null\", \"postcode\": \"string|null\",\n"
"  \"city\": \"string|null\", \"country\": \"string|null\", \"contactPhone\": \"string|null\",\n"
"  \"contactMail\": \"string|null\", \"vatId\": \"string|null\"\n"
"},\n"
"\"serviceProvider\": {\n"
"  \"companyName\": \"string|null\", \"address\": \"string|null\", \"postcode\": \"string|null\",\n"
"  \"city\": \"string|null\", \"country\": \"string|null\", \"contactPhone\": \"string|null\",\n"
"  \"contactMail\": \"string|null\", \"vatId\": \"string|null\"\n"
"},\n"
"\"payment\": { \"iban\": \"string|null\", \"bic\": \"string|null\", \"bankName\": \"string|null\", \"dueDate\": \"YYYY-MM-DD|null\" },\n"
"\"recipient\": {\n"
"  \"companyName\": \"string|null\", \"contactFirstname\": \"string|null\", \"contactName\": \"string|null\",\n"
"  \"street\": \"string|null\", \"postcode\": \"string|null\", \"city\": \"string|null\",\n"
"  \"country\": \"string|null\", \"contactPhone\": \"string|null\", \"contactMail\": \"string|null\"\n"
"},\n"
"\"policyholder\": { \"name\": \"string|null\", \"address\": \"string|null\", \"postcode\": \"string|null\", \"city\": \"string|null\", \"country\": \"string|null\" },\n"
"\"damageLocation\": { \"address\": \"string|null\", \"postcode\": \"string|null\", \"city\": \"string|null\", \"country\": \"string|null\" },\n"
"\"items\": [\n"
"  {\n"
"    \"position\": \"string|null\",\n"
"    \"name\": \"string|null\",\n"
"    \"qty\": \"number|null\",\n"
"    \"unit\": \"string|null\",\n"
"    \"unitCode\": \"integer (0-30, default 0)\",\n"
"    \"unitPriceNet\": \"number|null\",\n"
"    \"lineTotalNet\": \"number|null\",\n"
"    \"taxRate\": \"number|null\",\n"
"    \"discount\": \"number|null\",\n"
"    \"source\": { \"snippet\": \"string\" }\n"
"  }\n"
"],\n"
"\"totals\": { \"net\": \"number|null\", \"tax\": { \"rate\": \"number|null\", \"amount\": \"number|null\" }, \"gross\": \"number|null\", \"discount\": \"number|null\" },\n"
"\"warnings\": [\"string\"]\n"
"}\n\n"
"Nur das vollständige JSON-Objekt ausgeben, ohne Erklärung oder Markdown.\n"
"Wenn du unsicher bist, gib den wahrscheinlichsten Wert und eine kurze Begründung in warnings."
    )
```

> Note on braces: unlike vetcostcheck's prompt (which doubles `{{`/`}}` as a leftover from an old `.format()` style), this prompt uses single braces in plain (non-f) string lines, so the example schema renders with clean single braces. Only the `items_hint` and `ocr_text` lines are f-strings, and neither contains literal braces.

- [ ] **Step 2: Verify it builds a string and accepts `subdocument_context`**

```bash
python -c "
from products.bps.extract_prompt import build_extract_prompt
p = build_extract_prompt(ocr_text='dummy', subdocument_context=None, expected_items=3)
assert isinstance(p, str) and len(p) > 500
assert 'Sachversicherung' in p and 'unitCode' in p  # BPS markers present
print('len', len(p))
"
```
Expected: prints `len <number>` over 500, no exception.

- [ ] **Step 3: Commit**

```bash
git add products/bps/extract_prompt.py
git commit -m "Add BPS extraction prompt (v1)"
```

---

## Task 5: Add the BPS analyze/split override

Adapts vetcostcheck's analyze prompt for BPS: BPS terminology, animal questions removed, email/cover-page awareness added. The output JSON keys stay `pages_with_invoice_information`, `number_of_invoices`, `invoice_pages`, `invoice_number_of_items` (core compatibility — see spec §3.1). No `subdocument_context` is produced.

**Files:**
- Create: `products/bps/analyze_overrides.py`

- [ ] **Step 1: Write the analyze override**

Create `products/bps/analyze_overrides.py`:

```python
"""BPS-specific analyze/split prompt + schema.

Splits a (possibly multi-Beleg) PDF into sub-documents. Adapted from the vet
analyze prompt: BPS terminology, no animal questions, plus a rule that email /
cover pages are not independent Belege. Output JSON keys are kept identical to
vet (pages_with_invoice_information, number_of_invoices, invoice_pages,
invoice_number_of_items) because core/pipeline.py consumes those exact keys.
"""
from __future__ import annotations


_ANALYZE_PROMPT_TEMPLATE = (
    "Du bist ein Experte für die Analyse von Handwerker-Belegen (Rechnungen und Angeboten) "
    "im Bereich der Sachversicherung (Hausrat / Wohngebäude, 'Belegprüfung Sach').\n"
    "\n"
    "Du bekommst ein Dokument sowohl als Bilder (ein Bild pro Seite) als auch im Markdown-Format "
    "mit Seitennummern (Beispiel '--- PAGE 1 --- ...'). Das Dokument kann einen oder mehrere Belege "
    "(Rechnungen oder Angebote) enthalten. Manche Belege erstrecken sich über mehrere Seiten; manche "
    "Dokumente enthalten zusätzlich eine weiterleitende E-Mail oder ein Anschreiben. Ggf. kann eine "
    "Seite auch unbrauchbar sein.\n"
    "\n"
    "WICHTIGE REGELN für die Erkennung von Beleggrenzen:\n"
    "- Unterschiedliche Belegnummern (Rechnungs-/Angebotsnummern) bedeuten IMMER separate Belege, "
    "auch wenn Absender und Empfänger identisch sind.\n"
    "- Unterschiedliche Belegdaten vom selben Absender deuten auf separate Belege hin.\n"
    "- Gleicher Absender + gleicher Empfänger bedeutet NICHT automatisch derselbe Beleg. Achte auf "
    "eindeutige Kennzeichen wie Belegnummer und Datum.\n"
    "- Seiten, die nur eine weiterleitende E-Mail, ein Anschreiben, Datenschutzhinweise oder "
    "Zahlungsterminal-Belege enthalten, sind KEINE eigenständigen Belege. Sie liefern höchstens "
    "Kontext (z. B. Versicherungsnehmer oder Schadenort) und werden dem zugehörigen Beleg zugeordnet "
    "oder ignoriert.\n"
    "- Eine einzelne gescannte Seite kann das Ende eines Belegs und den Anfang eines anderen enthalten. "
    "Weise die Seite dem Beleg mit dem größten inhaltlichen Anteil zu; bei wesentlichem Anteil beider "
    "weise sie beiden zu.\n"
    "- Nutze die Bilder, um visuelle Dokumentgrenzen zu erkennen: unterschiedliche Briefköpfe, Logos, "
    "Layouts oder Trennlinien deuten auf separate Belege hin.\n"
    "\n"
    "Deine Aufgabe ist es, das Dokument zu analysieren und folgende Fragen zu beantworten. Die "
    "Antworten sollen konsolidiert und im JSON-Format zurückgegeben werden.\n"
    " Frage 1: Welche Seiten enthalten nützliche Informationen zu einem Beleg (Rechnung/Angebot)? "
    "Output: 'pages_with_invoice_information': <list of page numbers>, z.B. [1,2,4,5]. Beachte: "
    "Seitenzahlen starten bei 1.\n"
    "Frage 2: Wie viele unabhängige Belege enthält das Dokument? Output: 'number_of_invoices': <number>.\n"
    "Frage 3: Welche Seiten gehören zu welchem Beleg? Output: 'invoice_pages': {{<beleg_number>: "
    "<list of page numbers>, ...}}.\n"
    "Frage 4: Wie viele Positionen (Zeilen in der Leistungs-/Positionstabelle) befinden sich auf "
    "jedem Beleg? Output: 'invoice_number_of_items': {{<beleg_number>: <number of positions>, ...}}.\n"
    "\n Hier ist das Dokument im Markdown-Format: {markdown_text} "
)


def build_analyze_prompt(*, markdown_text: str = "") -> str:
    """Build the BPS analyze/split prompt."""
    return _ANALYZE_PROMPT_TEMPLATE.format(markdown_text=markdown_text)


ANALYZE_OUTPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "BPS analyze output",
    "description": "BPS splitting/analysis output. Keys match core's expectations; no per-subdocument context is produced for BPS.",
    "type": "object",
    "properties": {
        "pages_with_invoice_information": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "number_of_invoices": {"type": "integer", "minimum": 0},
        "invoice_pages": {
            "type": "object",
            "description": "Map of <beleg_number> (str) -> list of page numbers",
            "additionalProperties": {"type": "array", "items": {"type": "integer"}},
        },
        "invoice_number_of_items": {
            "type": "object",
            "description": "Map of <beleg_number> (str) -> count of positions on that Beleg",
            "additionalProperties": {"type": "integer"},
        },
    },
}
```

- [ ] **Step 2: Verify it builds and the template substitutes**

```bash
python -c "
from products.bps.analyze_overrides import build_analyze_prompt, ANALYZE_OUTPUT_SCHEMA
p = build_analyze_prompt(markdown_text='--- PAGE 1 --- hallo')
assert '--- PAGE 1 --- hallo' in p
assert 'invoice_pages' in p and 'Belegprüfung' in p
assert set(ANALYZE_OUTPUT_SCHEMA['properties']) >= {'pages_with_invoice_information','number_of_invoices','invoice_pages','invoice_number_of_items'}
print('ok')
"
```
Expected: `ok` (confirms `{{`/`}}` escaping is correct — the markdown substituted and the literal `{...}` examples survived).

- [ ] **Step 3: Commit**

```bash
git add products/bps/analyze_overrides.py
git commit -m "Add BPS analyze/split override"
```

---

## Task 6: Add the BPS `ProductConfig`

**Files:**
- Create: `products/bps/product.py`

- [ ] **Step 1: Write `product.py`**

Create `products/bps/product.py` (mirrors `products/vetcostcheck/product.py`):

```python
"""bps ProductConfig — Belegprüfung Sach extraction."""
from __future__ import annotations

import json
from pathlib import Path

from core.product import ProductConfig
from products.bps.analyze_overrides import (
    ANALYZE_OUTPUT_SCHEMA,
    build_analyze_prompt,
)
from products.bps.extract_prompt import build_extract_prompt

_HERE = Path(__file__).resolve().parent

with (_HERE / "extract_schema.json").open() as fh:
    _EXTRACT_SCHEMA = json.load(fh)


CONFIG = ProductConfig(
    name="bps",
    extract_prompt_builder=build_extract_prompt,
    extract_output_schema=_EXTRACT_SCHEMA,
    analyze_prompt_builder=build_analyze_prompt,
    analyze_output_schema=ANALYZE_OUTPUT_SCHEMA,
)
```

- [ ] **Step 2: Verify the config loads**

```bash
PRODUCT_NAME=bps python -c "
from core.product import load_product_config
c = load_product_config()
print(c.name, callable(c.extract_prompt_builder), callable(c.analyze_prompt_builder), list(c.extract_output_schema['properties'])[:4])
"
```
Expected: `bps True True [...]` with no errors.

- [ ] **Step 3: Commit**

```bash
git add products/bps/product.py
git commit -m "Add BPS ProductConfig"
```

---

## Task 7: Add BPS smoke tests

**Files:**
- Create: `tests/products/bps/__init__.py`
- Create: `tests/products/bps/test_smoke.py`

- [ ] **Step 1: Write the smoke test**

```bash
touch tests/products/bps/__init__.py
```

Create `tests/products/bps/test_smoke.py`:

```python
"""Smoke test for the bps product. Mirrors the vetcostcheck smoke test."""
from core.product import load_product_config


def test_bps_config_loads(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    assert config.name == "bps"
    assert callable(config.extract_prompt_builder)
    assert callable(config.analyze_prompt_builder)
    assert isinstance(config.extract_output_schema, dict)
    assert config.extract_output_schema  # non-empty


def test_bps_extract_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    # subdocument_context must be accepted and ignored by BPS.
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", subdocument_context=None)
    assert isinstance(prompt, str)
    assert len(prompt) > 500


def test_bps_analyze_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bps")
    config = load_product_config()
    prompt = config.analyze_prompt_builder(markdown_text="--- PAGE 1 --- x")
    assert isinstance(prompt, str)
    assert "invoice_pages" in prompt
```

- [ ] **Step 2: Run the tests**

Run: `python -m pytest tests/products/bps/test_smoke.py -v`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/products/bps/__init__.py tests/products/bps/test_smoke.py
git commit -m "Add BPS smoke tests"
```

---

## Task 8: Local end-to-end validation and prompt iteration

Run the real pipeline against all seven BPS samples locally and refine the prompt until the output is correct. No Azure resources involved. Prompt tuning is iterative — commit improvements to `products/bps/extract_prompt.py` / `analyze_overrides.py` as you go.

**Files:**
- Create: `scripts/extract_local.py`

- [ ] **Step 1: Write a local runner**

Create `scripts/extract_local.py`:

```python
"""Run the full extraction pipeline on one local PDF for any product.

Usage:
    PRODUCT_NAME=bps STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \\
        python scripts/extract_local.py path/to/file.pdf
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/extract_local.py <pdf>", file=sys.stderr)
        return 2
    pdf = Path(sys.argv[1])
    if not pdf.exists():
        print(f"No such file: {pdf}", file=sys.stderr)
        return 2

    from core.storage.file_storage import save_upload
    from core.jobs.tasks import process_file

    with pdf.open("rb") as fh:
        file_id = save_upload(fh.read(), original_filename=pdf.name)
    result = process_file(file_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> If `save_upload`'s signature differs from `(file_bytes, original_filename=...)`, check `core/storage/file_storage.py` and match it (the regression check uses the same helper).

- [ ] **Step 2: Run one sample and inspect the output**

```bash
docker compose up redis -d
set -a; source .env; set +a
PRODUCT_NAME=bps STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
  python scripts/extract_local.py bps_sanierer_input/BPS_Input/BPS_2.pdf | tee /tmp/bps_2.json
```

Expected: a JSON object `{ "number_of_subdocuments": 1, "subdocuments": [ { ... } ] }`. For `BPS_2.pdf` specifically verify: the email page (page 1) is NOT a separate subdocument; `type` = `quote`; `sender.companyName` ≈ "Stadler"; `payment.iban` present; 3 items with `qty`/`unitPriceNet`/`lineTotalNet`; `unit`=null + `unitCode`=0 (no unit column); `totals` net 4246.00 / tax 806.74 / gross 5052.74; and a warning that the Schadenort (Irene Seeger / Ettlingen, from the Betreff) differs from the Rechnungsanschrift (Volker Steinbach / Leinfelden).

- [ ] **Step 3: Run all seven samples**

```bash
for f in bps_sanierer_input/BPS_Input/BPS_*.pdf; do
  echo "=== $f ==="
  PRODUCT_NAME=bps STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
    python scripts/extract_local.py "$f" | tee "/tmp/$(basename "$f").json" | python -c "import sys,json; d=json.load(sys.stdin); print('subdocs:', d['number_of_subdocuments'], '| items per doc:', [len(s.get('items',[])) for s in d['subdocuments']])"
done
```

Expected: each runs without error and prints a subdocument count + per-doc item counts. `BPS_3.pdf` (7.7 MB) is the multi-page stress case.

- [ ] **Step 4: Triage and refine**

For each sample, check against the Erfassungsmaske fields (spec §4): Belegart, Belegnummer, Belegdatum, sender/serviceProvider, policyholder, damageLocation, per-position `position`/`name`/`qty`/`unit`/`unitCode`/prices, totals arithmetic. Where the model is wrong, sharpen the rule wording in `products/bps/extract_prompt.py` (or splitting in `analyze_overrides.py`), re-run that sample, and commit:

```bash
git add products/bps/extract_prompt.py products/bps/analyze_overrides.py
git commit -m "Refine BPS prompt: <what changed>"
```

Iterate until output quality is acceptable across all seven samples. Note any systematic gaps (e.g. a unit not in the 0–30 enum, a recurring Schadenort miss) in the spec's open-questions section for expert review.

- [ ] **Step 5: Commit the local runner**

```bash
git add scripts/extract_local.py
git commit -m "Add scripts/extract_local.py for per-product local extraction"
```

---

## Task 9: Build, provision, and verify the BPS Container Apps

Stand up `ca-api-bps` / `ca-worker-bps` alongside everything else. No custom domain yet (deferred to the later batch).

**Files:** none (deployment-only).

- [ ] **Step 1: Build and push the BPS image**

```bash
TAG="v20260529a"
./deploy.sh bps "$TAG"
```

Expected: image `cr3cinvoice.azurecr.io/3cix-bps:v20260529a` builds in ACR; the app-update steps SKIP (`ca-api-bps` / `ca-worker-bps` don't exist yet). Confirm:

```bash
az acr repository show-tags --name cr3cinvoice --repository 3cix-bps -o tsv
```
Expected: `v20260529a` listed.

- [ ] **Step 2: Provision the BPS Container Apps**

`provision_product.sh` needs the Redis connection details (not in `.env`) and a Sentry DSN (reuse production's). A fresh `INVOICE_API_KEY` is fine for BPS — there is no existing client contract to preserve.

```bash
set -a; source .env 2>/dev/null; set +a
export REDIS_URL="$(az containerapp secret show --name ca-invoice-worker --resource-group rg-3c-invoice --secret-name redis-url --query value -o tsv)"
export REDIS_PASSWORD="$(az redis list-keys --name redis-3c-invoice-v2 --resource-group rg-3c-invoice --query primaryKey -o tsv)"
export KEDA_REDIS_HOST="redis-3c-invoice-v2.redis.cache.windows.net:6380"
export SENTRY_DSN="$(az containerapp secret show --name ca-invoice-api --resource-group rg-3c-invoice --secret-name sentry-dsn --query value -o tsv)"
unset INVOICE_API_KEY   # let the script generate a fresh per-product key
bash scripts/provision_product.sh bps v20260529a
```

Expected: `ca-api-bps` and `ca-worker-bps` are created with a KEDA scaler on `rq:queue:jobs-bps`. Record the printed API FQDN and the generated `INVOICE_API_KEY`.

- [ ] **Step 3: Verify health**

```bash
FQDN=$(az containerapp show --name ca-api-bps --resource-group rg-3c-invoice --query "properties.configuration.ingress.fqdn" -o tsv)
curl -s "https://${FQDN}/healthz"; echo
curl -s "https://${FQDN}/ready"; echo
```
Expected: `{"status":"ok"}` and `{"status":"ok","checks":{"redis":"ok","storage":"ok"}}`.

- [ ] **Step 4: End-to-end test through the deployed BPS API**

```bash
FQDN=$(az containerapp show --name ca-api-bps --resource-group rg-3c-invoice --query "properties.configuration.ingress.fqdn" -o tsv)
BPS_KEY="<the INVOICE_API_KEY printed in Step 2>"
API_BASE="https://${FQDN}" INVOICE_API_KEY="$BPS_KEY" \
  .venv/bin/python - <<'PY'
import os, time, requests
base, key = os.environ["API_BASE"], os.environ["INVOICE_API_KEY"]
h = {"X-API-Key": key}
with open("bps_sanierer_input/BPS_Input/BPS_2.pdf", "rb") as f:
    fid = requests.post(f"{base}/upload", files={"file": f}, headers=h).json()["file_id"]
job = requests.post(f"{base}/process", json={"file_id": fid}, headers=h).json()["job_id"]
while True:
    d = requests.get(f"{base}/job/{job}", headers=h).json()
    print("status:", d["status"])
    if d["status"] in ("finished", "failed"):
        print(d.get("result") or d.get("error")); break
    time.sleep(10)
PY
```

Expected: the job is enqueued on `jobs-bps`, the worker cold-starts from zero, and the result matches the local Task 8 output for `BPS_2.pdf`.

- [ ] **Step 5: No commit** (deployment-only). The custom-domain cutover (`3cbps.flex-capital-scale.com`) is intentionally deferred to the later batch alongside Sanierer and the pending vetcostcheck Task 16.

---

## Self-Review Checklist

- [ ] `core/` no longer references `animal_information` / `invoice_animals` (except the intentional pass-through into legacy `build_prompt_from_config`). Vet regression check passes post-refactor.
- [ ] `products/bps/` has `__init__.py`, `extract_schema.json`, `extract_prompt.py`, `analyze_overrides.py`, `product.py`; `PRODUCT_NAME=bps` loads a valid `ProductConfig`.
- [ ] BPS extract builder accepts and ignores `subdocument_context`; analyze override emits `invoice_pages` etc. (core-compatible keys), no `subdocument_context`.
- [ ] Output schema matches spec §4.2 (two parties, policyholder, damageLocation, item `position`/`unit`+`unitCode`/`taxRate`/`discount`); unit enum 0–30 with default 0.
- [ ] All seven BPS samples run locally without error and have been triaged.
- [ ] `ca-api-bps` / `ca-worker-bps` provisioned, healthy, e2e verified; no custom domain bound yet.
- [ ] BPS smoke tests pass (`pytest tests/products/bps/`).

## Out of Scope (separate work)

- **Custom-domain cutover** for BPS, Sanierer, and the pending vetcostcheck Task 16 — batched later (DNS + managed certs in one sitting).
- **Sanierer product** — its own spec + plan ("analog BPS" + an `lvPosition` item field; line items only, header from the Auftrag).
- **Schadennummer / claim-reference extraction** — deferred pending expert validation (spec §7).
