# Sanierer Product Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Onboard `sanierer` (Schadensanierung restoration-contractor documents) as a new product — items-focused extraction with a per-item LV-Position — from `products/sanierer/` through to deployed Container Apps, leaving the custom-domain cutover for a later batch.

**Architecture:** Add `products/sanierer/` mirroring the existing `products/bps/` (schema + extraction prompt + analyze/split override + `ProductConfig`). No `core/` changes — the platform is already product-agnostic and domain-neutral (the `subdocument_context` seam landed during BPS onboarding). `deploy.sh`, `scripts/provision_product.sh`, and the `Dockerfile` are reused unchanged.

**Tech Stack:** Python 3.11, FastAPI, RQ, Redis, Azure Container Apps, Azure OpenAI, Azure Document Intelligence, Mistral OCR, Azure Blob Storage. Domain language: German.

**Spec:** `docs/superpowers/specs/2026-05-29-sanierer-extraction-design.md`

**Environment notes (learned during BPS onboarding):**
- Bare `python` is not on PATH — use `.venv/bin/python`.
- `process_file` runs synchronously and needs **no live Redis** (set `REDIS_URL` only because config reads it). No Docker required for local runs.
- `scripts/extract_local.py` already exists and routes structlog to stderr, so stdout is clean JSON.

---

## File Structure

**New product (Tasks 1–6) — create:**
- `products/sanierer/__init__.py`
- `products/sanierer/extract_schema.json` — items-focused output schema (+ `lvPosition`).
- `products/sanierer/extract_prompt.py` — `build_extract_prompt(...)` German extraction prompt.
- `products/sanierer/analyze_overrides.py` — `build_analyze_prompt(...)` + `ANALYZE_OUTPUT_SCHEMA`.
- `products/sanierer/product.py` — exports `CONFIG: ProductConfig`.
- `tests/products/sanierer/__init__.py`, `tests/products/sanierer/test_smoke.py`

**Validation (Task 7):** reuse `scripts/extract_local.py` (no new files).

**Deployment (Task 8) — no file changes:** `deploy.sh` already lists `sanierer`; `scripts/provision_product.sh` and `Dockerfile` are product-generic.

---

## Task 1: Create the `products/sanierer/` package skeleton

**Files:**
- Create: `products/sanierer/__init__.py`

- [ ] **Step 1: Create the package**

```bash
touch products/sanierer/__init__.py
```

- [ ] **Step 2: Verify it imports**

Run: `.venv/bin/python -c "import products.sanierer; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add products/sanierer/__init__.py
git commit -m "Add empty products/sanierer/ package"
```

---

## Task 2: Add the Sanierer output schema

**Files:**
- Create: `products/sanierer/extract_schema.json`

- [ ] **Step 1: Write the schema**

Create `products/sanierer/extract_schema.json` (items-focused; per spec §4):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Sanierer extraction output",
  "description": "Items-focused Beleg data extracted from a single Sanierer (Schadensanierung) sub-document. Header/party data is intentionally omitted (it comes from the Auftrag). Documentation + future validation target; not enforced against the LLM output at runtime. Wrapped by the pipeline in {number_of_subdocuments, subdocuments[]}.",
  "type": "object",
  "properties": {
    "type": {"type": ["string", "null"], "enum": ["invoice", "quote", null], "description": "Belegart: invoice=Rechnung, quote=Angebot"},
    "currency": {"type": ["string", "null"], "description": "ISO 4217 code, e.g. EUR"},
    "number": {"type": ["string", "null"], "description": "Belegnummer (Angebots-/Rechnungsnummer)"},
    "issuedAt": {"type": ["string", "null"], "description": "Belegdatum, YYYY-MM-DD"},
    "items": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "position": {"type": ["string", "null"], "description": "document running position, e.g. 05.01.001."},
          "lvPosition": {"type": ["string", "null"], "description": "Leistungsverzeichnis reference, e.g. 05.04.001"},
          "name": {"type": ["string", "null"], "description": "Beschreibung (full position text)"},
          "qty": {"type": ["number", "null"], "description": "Menge (may be fractional for % positions)"},
          "unit": {"type": ["string", "null"], "description": "ME raw text, e.g. M2, ST, H, %"},
          "unitCode": {"type": "integer", "minimum": 0, "maximum": 30, "description": "0-30 enum; default 0 (PIECE)"},
          "unitPriceNet": {"type": ["number", "null"], "description": "Einzelpreis"},
          "lineTotalNet": {"type": ["number", "null"], "description": "Gesamtpreis (negative for Rabatt lines)"},
          "taxRate": {"type": ["number", "null"], "description": "per-line MwSt % (usually null)"},
          "discount": {"type": ["number", "null"], "description": "per-line Rabatt/Skonto (usually null)"},
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

Run: `.venv/bin/python -c "import json; d=json.load(open('products/sanierer/extract_schema.json')); print(sorted(d['properties'].keys())); print(sorted(d['properties']['items']['items']['properties'].keys()))"`
Expected: top-level `['currency', 'issuedAt', 'items', 'number', 'totals', 'type', 'warnings']`; item keys include `lvPosition` and `position`.

- [ ] **Step 3: Commit**

```bash
git add products/sanierer/extract_schema.json
git commit -m "Add Sanierer extraction output schema (items-only + lvPosition)"
```

---

## Task 3: Add the Sanierer extraction prompt (v1)

Complete, runnable German prompt. Mirrors the BPS prompt structure but is items-focused (no party/header fields) and adds the Sanierer rules: separate `position` vs `lvPosition`, skip hierarchical Titel headers, handle %-positions. Expect refinement during local iteration (Task 7).

**Files:**
- Create: `products/sanierer/extract_prompt.py`

- [ ] **Step 1: Write the prompt builder**

Create `products/sanierer/extract_prompt.py`:

```python
"""Sanierer (Schadensanierung) extraction prompt (German).

Items-focused: extracts Belegpositionen (with an extra LV-Position) plus totals;
header/party data is intentionally not extracted (it comes from the Auftrag).
Mirrors the BPS prompt structure.
"""
from __future__ import annotations


def build_extract_prompt(
    *,
    ocr_text: str = "",
    subdocument_context: list[dict] | None = None,
    expected_items: int | None = None,
) -> str:
    """Build the extraction prompt for a single Sanierer sub-document.

    `subdocument_context` is accepted for signature compatibility with the
    pipeline but is unused for Sanierer (no per-subdocument context is produced).
    """
    if expected_items and expected_items > 0:
        items_hint = (
            f"WICHTIG: Dieser Beleg enthält voraussichtlich etwa {expected_items} abrechenbare Positionen. "
            f"Wenn du weniger als {expected_items} Positionen findest, überprüfe nochmals den OCR-Text und "
            f"das Bild — wahrscheinlich hast du Zeilen übersehen."
        )
    else:
        items_hint = ""

    return (
"Du bist ein Experte für die Prüfung von Schadensanierungs-Belegen (Rechnungen und Angeboten) "
"im Bereich der Sachversicherung. Solche Belege sind nach einem Leistungsverzeichnis (LV) aufgebaut "
"und hierarchisch in Titel und Positionen gegliedert.\n"
"Deine Aufgabe ist es, aus dem untenstehenden Beleg die abrechenbaren Positionen und die Summen zu "
"extrahieren und sie ausschließlich als gültiges JSON-Objekt im definierten Schema zurückzugeben.\n"
"Erfinde keine Werte. Wenn ein Feld nicht sicher ermittelt werden kann, gib null zurück und erkläre "
"Unsicherheiten im Feld 'warnings'.\n"
f"{items_hint}\n"
"Der Beleg ist als Bild (visuelle Referenz) sowie als OCR-Text aus zwei unabhängigen OCR-Systemen "
"verfügbar. Der OCR-Text ist zwischen Doppel-Pipes (||) angegeben und enthält zwei mit 'OCR Source A' "
"und 'OCR Source B' gekennzeichnete Abschnitte. Nutze beide OCR-Quellen, um Fehler zu erkennen und zu "
"korrigieren. Bei Widersprüchen zwischen den OCR-Quellen überprüfe mit dem Bild.\n\n"
"OCR-Text:\n"
f"||\n{ocr_text}\n||\n\n"
"Regeln für die Extraktion:\n"
"1. Keine Halluzinationen: Nur Werte extrahieren, die im OCR- oder Bildinhalt sichtbar oder eindeutig ableitbar sind.\n"
"2. Wenn ein Feld fehlt oder nicht eindeutig ist → null.\n"
"3. Strings: ohne führende/trailing Leerzeichen.\n"
"4. Geldbeträge: nur Ziffern und Punkt als Dezimaltrennzeichen, z. B. 408.10. Tausenderpunkte entfernen. Negative Beträge (Rabatt/Gutschrift) als negative Zahl behalten.\n"
"5. Datumsformat: YYYY-MM-DD.\n"
"6. Währung: ISO-4217-Code (z. B. \"EUR\").\n"
"7. Belegart ('type'): 'invoice' für eine Rechnung, 'quote' für ein Angebot. Wenn unklar → null.\n"
"8. HIERARCHIE: Sanierer-Belege sind in Titel (z. B. '01. Allgemeines'), Unter-Titel (z. B. '01.01. Einrichtung Baustelle') und abrechenbare Einzelpositionen (z. B. '05.01.001.') gegliedert. NUR abrechenbare Einzelpositionen mit Menge und Preis sind items. Titel- und Unter-Titel-Überschriften (ohne Menge/Preis) sind KEINE items.\n"
"9. ZWEI Positionsnummern je Position: 'position' = die laufende Positionsnummer dieses Belegs (z. B. '05.01.001.'); 'lvPosition' = die LV-Nummer (Leistungsverzeichnis-Referenz), die meist am Anfang der ausführlichen Beschreibungszeile steht (z. B. '05.04.001') und sich in der Regel von 'position' unterscheidet. Wenn nur eine Nummer vorhanden ist, setze 'position' und lasse 'lvPosition' null.\n"
"10. 'name': der vollständige Beschreibungstext der Position (ggf. über mehrere Zeilen).\n"
"11. Einheit: 'unit' = Rohtext der Spalte ME (z. B. 'M2', 'ST', 'H', '%'); 'unitCode' = passender Code aus der folgenden Liste. Wenn keine Einheit angegeben ist oder keine passt, setze unit=null und unitCode=0 (Stück).\n"
"    0=Stk, 1=mm, 2=mm², 3=mm³, 4=cm, 5=cm², 6=cm³, 7=m, 8=m², 9=m³, 10=Woche, 11=Monat, 12=kg, 13=Std, 14=Tag, 15=km, 16=%, 17=l, 18=lm, 19=pauschal, 20=kWh, 21=Paar, 22=t, 23=AW, 24=Satz, 25=Stange, 26=g, 27=StWo, 28=Sonstige, 29=Kilowatt Peak, 30=Grad.\n"
"    Hinweis: Spaltenwerte 'M2'→8 (m²), 'ST'→0 (Stück), 'H'→13 (Std), '%'→16, 'M'→7, 'lfm'/'lm'→18.\n"
"12. Prozent-/Pauschal-/Rabattpositionen (z. B. Aufwandspauschale, Regiekosten, AXA-Rabatt): unit='%', unitCode=16, qty=der angegebene Bruchwert (z. B. 0.010 für '0,010 %'), und 'lineTotalNet' wie ausgewiesen (negativ bei Rabatt). Diese Zeilen sind items.\n"
"13. 'unitPriceNet' = Einzelpreis, 'lineTotalNet' = Gesamtpreis der Zeile.\n"
"14. taxRate/discount je Position nur, wenn explizit je Zeile angegeben; sonst null.\n"
"15. KEINE items: 'Übertrag', 'Zusammenstellung Titel', 'Summe ...', 'SE Basis', 'Nettogesamtpreis', 'Umsatzsteuer', 'Gesamtsumme', Titel-/Unter-Titel-Überschriften.\n"
"16. Totals: 'Nettogesamtpreis' → totals.net; 'Umsatzsteuer' (Satz und Betrag) → totals.tax.rate / totals.tax.amount; 'Gesamtsumme' → totals.gross. Ein Rabatt auf Belegebene → totals.discount.\n"
"17. Validierung: Die Summe aller items.lineTotalNet sollte ≈ totals.net sein (Toleranz ±0.02, Rabattzeilen mindern die Summe). Außerdem totals.net + totals.tax.amount ≈ totals.gross. Vermerke Abweichungen in 'warnings'.\n"
"18. Quellreferenzen: gib einen kurzen Textausschnitt der extrahierten Zeile in source.snippet an.\n"
"19. Alle Positionen in der Reihenfolge des Belegs extrahieren — nicht zusammenfassen, nicht deduplizieren.\n\n"
"JSON-Ziel-Schema:\n"
"{\n"
"\"type\": \"invoice|quote|null\",\n"
"\"currency\": \"EUR|null\",\n"
"\"number\": \"string|null\",\n"
"\"issuedAt\": \"YYYY-MM-DD|null\",\n"
"\"items\": [\n"
"  {\n"
"    \"position\": \"string|null\",\n"
"    \"lvPosition\": \"string|null\",\n"
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

- [ ] **Step 2: Verify it builds a string and accepts `subdocument_context`**

```bash
.venv/bin/python -c "
from products.sanierer.extract_prompt import build_extract_prompt
p = build_extract_prompt(ocr_text='dummy', subdocument_context=None, expected_items=5)
assert isinstance(p, str) and len(p) > 500
assert 'lvPosition' in p and 'Leistungsverzeichnis' in p and 'unitCode' in p
print('len', len(p))
"
```
Expected: prints `len <number>` over 500, no exception.

- [ ] **Step 3: Commit**

```bash
git add products/sanierer/extract_prompt.py
git commit -m "Add Sanierer extraction prompt (v1)"
```

---

## Task 4: Add the Sanierer analyze/split override

The BPS analyze override retermed for Schadensanierung. Output JSON keys stay `pages_with_invoice_information`, `number_of_invoices`, `invoice_pages`, `invoice_number_of_items` (core compatibility). No `subdocument_context`.

**Files:**
- Create: `products/sanierer/analyze_overrides.py`

- [ ] **Step 1: Write the analyze override**

Create `products/sanierer/analyze_overrides.py`:

```python
"""Sanierer-specific analyze/split prompt + schema.

Splits a (possibly multi-Beleg) PDF into sub-documents. Adapted from the BPS
analyze prompt: Schadensanierung terminology, no animal questions, with a rule
that email / cover pages are not independent Belege. Output JSON keys are kept
identical to vet/BPS (pages_with_invoice_information, number_of_invoices,
invoice_pages, invoice_number_of_items) because core/pipeline.py consumes those
exact keys.
"""
from __future__ import annotations


_ANALYZE_PROMPT_TEMPLATE = (
    "Du bist ein Experte für die Analyse von Schadensanierungs-Belegen (Rechnungen und Angeboten) "
    "im Bereich der Sachversicherung.\n"
    "\n"
    "Du bekommst ein Dokument sowohl als Bilder (ein Bild pro Seite) als auch im Markdown-Format "
    "mit Seitennummern (Beispiel '--- PAGE 1 --- ...'). Das Dokument kann einen oder mehrere Belege "
    "(Rechnungen oder Angebote) enthalten. Ein Beleg erstreckt sich häufig über mehrere Seiten; manche "
    "Dokumente enthalten zusätzlich eine weiterleitende E-Mail oder ein Anschreiben. Ggf. kann eine "
    "Seite auch unbrauchbar sein.\n"
    "\n"
    "WICHTIGE REGELN für die Erkennung von Beleggrenzen:\n"
    "- Unterschiedliche Belegnummern (Angebots-/Rechnungsnummern) bedeuten IMMER separate Belege, "
    "auch wenn Absender und Empfänger identisch sind.\n"
    "- Unterschiedliche Belegdaten vom selben Absender deuten auf separate Belege hin.\n"
    "- Mehrseitige Belege mit fortlaufenden Positionsnummern und 'Übertrag'-Zeilen gehören zu EINEM "
    "Beleg; eine neue Seite beginnt nicht automatisch einen neuen Beleg.\n"
    "- Seiten, die nur eine weiterleitende E-Mail, ein Anschreiben, Datenschutzhinweise oder "
    "Zahlungsterminal-Belege enthalten, sind KEINE eigenständigen Belege und werden dem zugehörigen "
    "Beleg zugeordnet oder ignoriert.\n"
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
    "Frage 4: Wie viele abrechenbare Positionen (Einzelpositionen mit Preis, ohne Titel-Überschriften) "
    "befinden sich auf jedem Beleg? Output: 'invoice_number_of_items': {{<beleg_number>: <number of "
    "positions>, ...}}.\n"
    "\n Hier ist das Dokument im Markdown-Format: {markdown_text} "
)


def build_analyze_prompt(*, markdown_text: str = "") -> str:
    """Build the Sanierer analyze/split prompt."""
    return _ANALYZE_PROMPT_TEMPLATE.format(markdown_text=markdown_text)


ANALYZE_OUTPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Sanierer analyze output",
    "description": "Sanierer splitting/analysis output. Keys match core's expectations; no per-subdocument context is produced for Sanierer.",
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
.venv/bin/python -c "
from products.sanierer.analyze_overrides import build_analyze_prompt, ANALYZE_OUTPUT_SCHEMA
p = build_analyze_prompt(markdown_text='--- PAGE 1 --- hallo')
assert '--- PAGE 1 --- hallo' in p
assert 'invoice_pages' in p and 'Schadensanierung' in p
assert set(ANALYZE_OUTPUT_SCHEMA['properties']) >= {'pages_with_invoice_information','number_of_invoices','invoice_pages','invoice_number_of_items'}
print('ok')
"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add products/sanierer/analyze_overrides.py
git commit -m "Add Sanierer analyze/split override"
```

---

## Task 5: Add the Sanierer `ProductConfig`

**Files:**
- Create: `products/sanierer/product.py`

- [ ] **Step 1: Write `product.py`**

Create `products/sanierer/product.py` (mirrors `products/bps/product.py`):

```python
"""sanierer ProductConfig — Schadensanierung Beleg extraction."""
from __future__ import annotations

import json
from pathlib import Path

from core.product import ProductConfig
from products.sanierer.analyze_overrides import (
    ANALYZE_OUTPUT_SCHEMA,
    build_analyze_prompt,
)
from products.sanierer.extract_prompt import build_extract_prompt

_HERE = Path(__file__).resolve().parent

with (_HERE / "extract_schema.json").open() as fh:
    _EXTRACT_SCHEMA = json.load(fh)


CONFIG = ProductConfig(
    name="sanierer",
    extract_prompt_builder=build_extract_prompt,
    extract_output_schema=_EXTRACT_SCHEMA,
    analyze_prompt_builder=build_analyze_prompt,
    analyze_output_schema=ANALYZE_OUTPUT_SCHEMA,
)
```

- [ ] **Step 2: Verify the config loads**

```bash
PRODUCT_NAME=sanierer .venv/bin/python -c "
from core.product import load_product_config
c = load_product_config()
print(c.name, callable(c.extract_prompt_builder), callable(c.analyze_prompt_builder), list(c.extract_output_schema['properties'])[:4])
"
```
Expected: `sanierer True True ['type', 'currency', 'number', 'issuedAt']` with no errors.

- [ ] **Step 3: Commit**

```bash
git add products/sanierer/product.py
git commit -m "Add Sanierer ProductConfig"
```

---

## Task 6: Add Sanierer smoke tests

**Files:**
- Create: `tests/products/sanierer/__init__.py`
- Create: `tests/products/sanierer/test_smoke.py`

- [ ] **Step 1: Write the smoke test**

```bash
touch tests/products/sanierer/__init__.py
```

Create `tests/products/sanierer/test_smoke.py`:

```python
"""Smoke test for the sanierer product. Mirrors the bps smoke test."""
from core.product import load_product_config


def test_sanierer_config_loads(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "sanierer")
    config = load_product_config()
    assert config.name == "sanierer"
    assert callable(config.extract_prompt_builder)
    assert callable(config.analyze_prompt_builder)
    assert isinstance(config.extract_output_schema, dict)
    assert config.extract_output_schema  # non-empty


def test_sanierer_extract_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "sanierer")
    config = load_product_config()
    # subdocument_context must be accepted and ignored by Sanierer.
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", subdocument_context=None)
    assert isinstance(prompt, str)
    assert len(prompt) > 500
    assert "lvPosition" in prompt


def test_sanierer_analyze_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "sanierer")
    config = load_product_config()
    prompt = config.analyze_prompt_builder(markdown_text="--- PAGE 1 --- x")
    assert isinstance(prompt, str)
    assert "invoice_pages" in prompt
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/python -m pytest tests/products/sanierer/test_smoke.py -v`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/products/sanierer/__init__.py tests/products/sanierer/test_smoke.py
git commit -m "Add Sanierer smoke tests"
```

---

## Task 7: Local end-to-end validation and prompt iteration

Run the real pipeline against all seven Sanierer samples locally and refine the prompt until output is correct. No Azure resources. Commit prompt improvements as you go.

**Files:** none (reuses `scripts/extract_local.py`).

- [ ] **Step 1: Run one sample and inspect the output**

```bash
set -a; source .env 2>/dev/null; set +a
PRODUCT_NAME=sanierer STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
  .venv/bin/python scripts/extract_local.py "bps_sanierer_input/Sanierer_Input/2520_910008880_Angebot.pdf" | tee /tmp/sanierer_angebot.json
```

Expected: a JSON object `{ "number_of_subdocuments": 1, "subdocuments": [ { ... } ] }`. For this Angebot specifically verify: `type` = `quote`; line items have BOTH `position` (e.g. "05.01.001.") and `lvPosition` (e.g. "05.04.001"); Titel headers (`01. Allgemeines`, `01.01. …`) are NOT items; the %-positions (Aufwandspauschale at `0.010`/`%`/code 16, AXA-Rabatt negative) are captured; `totals` net 3190.12 / tax 606.12 / gross 3796.24; and Σ items.lineTotalNet ≈ 3190.12.

- [ ] **Step 2: Run all seven samples**

```bash
set -a; source .env 2>/dev/null; set +a
for f in bps_sanierer_input/Sanierer_Input/*.pdf; do
  b=$(basename "$f")
  PRODUCT_NAME=sanierer STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 \
    .venv/bin/python scripts/extract_local.py "$f" > "/tmp/SAN_$b.json" 2>/dev/null \
    && .venv/bin/python -c "import json; d=json.load(open('/tmp/SAN_$b.json')); print('$b', 'subdocs:', d['number_of_subdocuments'], '| items:', [len(s.get('items',[])) for s in d['subdocuments']])" \
    || echo "FAIL $b"
done
```

Expected: each runs without error and prints a subdocument count + per-doc item counts.

- [ ] **Step 3: Triage and refine**

For each sample, check against spec §4/§5: Belegart, `position` vs `lvPosition` separation, Titel headers skipped, %-positions and negative Rabatt lines, unit mapping, and totals reconciliation (Σitems ≈ net; net+tax ≈ gross). Where the model is wrong, sharpen the rule wording in `products/sanierer/extract_prompt.py` (or splitting in `analyze_overrides.py`), re-run that sample, and commit:

```bash
git add products/sanierer/extract_prompt.py products/sanierer/analyze_overrides.py
git commit -m "Refine Sanierer prompt: <what changed>"
```

Note systematic gaps (e.g. a Beleg type beyond invoice/quote, a recurring lvPosition miss) for expert review; append them to `docs/bps-open-questions.md` or a new `docs/sanierer-open-questions.md` if substantive.

---

## Task 8: Build, provision, and verify the Sanierer Container Apps

Stand up `ca-api-sanierer` / `ca-worker-sanierer`. No custom domain yet (deferred to the later batch).

**Files:** none (deployment-only).

- [ ] **Step 1: Build and push the Sanierer image**

```bash
TAG="v20260529a"
./deploy.sh sanierer "$TAG"
```

Expected: image `cr3cinvoice.azurecr.io/3cix-sanierer:v20260529a` builds in ACR; the app-update steps SKIP (apps don't exist yet). Confirm:

```bash
az acr repository show-tags --name cr3cinvoice --repository 3cix-sanierer -o tsv
```
Expected: `v20260529a` listed.

- [ ] **Step 2: Provision the Sanierer Container Apps**

Redis details aren't in `.env`; reuse the existing infra values, and reuse production's Sentry DSN. A fresh `INVOICE_API_KEY` is fine (no existing client contract).

```bash
set -a; source .env 2>/dev/null; set +a
export REDIS_URL="$(az containerapp secret show --name ca-invoice-worker --resource-group rg-3c-invoice --secret-name redis-url --query value -o tsv)"
export REDIS_PASSWORD="$(az redis list-keys --name redis-3c-invoice-v2 --resource-group rg-3c-invoice --query primaryKey -o tsv)"
export KEDA_REDIS_HOST="redis-3c-invoice-v2.redis.cache.windows.net:6380"
export SENTRY_DSN="$(az containerapp secret show --name ca-invoice-api --resource-group rg-3c-invoice --secret-name sentry-dsn --query value -o tsv)"
unset INVOICE_API_KEY   # let the script generate a fresh per-product key
bash scripts/provision_product.sh sanierer v20260529a
```

Expected: `ca-api-sanierer` and `ca-worker-sanierer` created with a KEDA scaler on `rq:queue:jobs-sanierer`. Record the printed API FQDN and generated `INVOICE_API_KEY`.

- [ ] **Step 3: Verify health**

```bash
FQDN=$(az containerapp show --name ca-api-sanierer --resource-group rg-3c-invoice --query "properties.configuration.ingress.fqdn" -o tsv)
curl -s "https://${FQDN}/healthz"; echo
curl -s "https://${FQDN}/ready"; echo
```
Expected: `{"status":"ok"}` and `{"status":"ok","checks":{"redis":"ok","storage":"ok"}}`.

- [ ] **Step 4: End-to-end test through the deployed Sanierer API**

```bash
FQDN=$(az containerapp show --name ca-api-sanierer --resource-group rg-3c-invoice --query "properties.configuration.ingress.fqdn" -o tsv)
SAN_KEY="<the INVOICE_API_KEY printed in Step 2>"
API_BASE="https://${FQDN}" INVOICE_API_KEY="$SAN_KEY" .venv/bin/python - <<'PY'
import os, time, requests
base, key = os.environ["API_BASE"], os.environ["INVOICE_API_KEY"]
h = {"X-API-Key": key}
with open("bps_sanierer_input/Sanierer_Input/2520_910008880_Angebot.pdf", "rb") as f:
    fid = requests.post(f"{base}/upload", files={"file": f}, headers=h, timeout=60).json()["file_id"]
job = requests.post(f"{base}/process", json={"file_id": fid}, headers=h, timeout=60).json()["job_id"]
while True:
    d = requests.get(f"{base}/job/{job}", headers=h, timeout=60).json()
    print("status:", d["status"])
    if d["status"] in ("finished", "failed"):
        print(d.get("result") or d.get("error")); break
    time.sleep(10)
PY
```

Expected: the job enqueues on `jobs-sanierer`, the worker cold-starts from zero, and the result matches the local Task 7 output for the Angebot.

- [ ] **Step 5: No commit** (deployment-only). The custom-domain cutover (`3csanierer.flex-capital-scale.com`) is deferred to the later batch alongside BPS and the pending vetcostcheck Task 16.

---

## Self-Review Checklist

- [ ] `products/sanierer/` has `__init__.py`, `extract_schema.json`, `extract_prompt.py`, `analyze_overrides.py`, `product.py`; `PRODUCT_NAME=sanierer` loads a valid `ProductConfig`.
- [ ] Output schema is items-focused (no party/header objects) and each item has both `position` and `lvPosition`; unit enum 0–30 with default 0.
- [ ] Extract builder accepts and ignores `subdocument_context`; analyze override emits the core-required keys (`invoice_pages`, `invoice_number_of_items`), no `subdocument_context`.
- [ ] Prompt skips Titel/group headers, separates position vs lvPosition, and handles %-positions.
- [ ] All seven Sanierer samples run locally without error and have been triaged.
- [ ] `ca-api-sanierer` / `ca-worker-sanierer` provisioned, healthy, e2e verified; no custom domain bound yet.
- [ ] Sanierer smoke tests pass (`pytest tests/products/sanierer/`).

## Out of Scope (separate work)

- **Custom-domain cutover** for Sanierer, BPS, and the pending vetcostcheck Task 16 — batched later (DNS + managed certs in one sitting), including retiring the old `ca-invoice-api` / `ca-invoice-worker`.
- **Sanierer header-field extraction** — intentionally omitted (from the Auftrag); additive later if experts request it (BPS schema already models the fields).
