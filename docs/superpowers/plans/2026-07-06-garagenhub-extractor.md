# GaragenHub Fahrzeugausweis Extractor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, stateless synchronous FastAPI service that extracts structured keeper + vehicle data from a photo of a Swiss Fahrzeugausweis in a single vision call.

**Architecture:** One image in → normalize (format detect, HEIC/PDF convert, downscale to 1600px) → one `gpt-5.4` Azure OpenAI vision call (`detail: high`, temp 0, seed) → deterministic per-field validation + assembly → typed JSON out. No worker, no Redis, no storage; the image is held in memory only and never persisted.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, openai (AzureOpenAI), Pillow + pillow-heif + pypdfium2, pydantic / pydantic-settings, tenacity, structlog, pytest.

## Global Constraints

- **Target repo root:** `/Users/bernhardlangwallner/Documents/05 Coding/3C/garagenhub-extractor/`. Every `Create`/`Modify`/`Test` path below is relative to this root. All `git` commands run inside this repo (created in Task 1).
- **Python:** 3.11+.
- **Determinism:** every Azure OpenAI call uses `temperature=0, seed=42`.
- **Vision settings:** images sent with `detail: "high"`; downscaled so the longest side ≤ 1600px; sent as `image/jpeg`.
- **Model:** default deployment name `gpt-5.4` (env `OPENAI_VISION_MODEL`).
- **Retries:** Azure calls wrapped in tenacity — 3 attempts, exponential backoff min 2s / max 30s.
- **Privacy:** never write the uploaded image or any extracted personal data to disk or logs. Logs carry timings, token counts, and per-field validity flags only.
- **Auth:** all endpoints except `GET /healthz` require header `X-Api-Key` equal to env `EXTRACTOR_API_KEY`.
- **Field paths:** the authoritative dotted field paths (used by validation, confidence, and tests) are defined once in `app/prompt.py:FIELD_PATHS` and imported everywhere else.
- **Naming:** service/repo name is `garagenhub-extractor`; Azure Container App is `ca-garagenhub`.

---

### Task 1: Scaffold the new repository

**Files:**
- Create: `.gitignore`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `.env.example`
- Create: `README.md`
- Create: `pytest.ini`
- Create: `app/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)
- Create: `tests/fixtures/` (holds the two sample cards)
- Create: `docs/superpowers/specs/2026-07-06-fahrzeugausweis-extraction-design.md` (copied from source repo)
- Create: `docs/superpowers/plans/2026-07-06-garagenhub-extractor.md` (this file, copied)
- Test: `tests/test_smoke.py`

**Interfaces:**
- Produces: the repo skeleton every later task builds on; `tests/fixtures/Fahrzeugausweis.jpeg` and `.jpg` for the Task 10 integration test.

- [ ] **Step 1: Create the repo directory, git init, and copy design docs + sample images**

```bash
NEW=/Users/bernhardlangwallner/Documents/05\ Coding/3C/garagenhub-extractor
SRC=/Users/bernhardlangwallner/Documents/05\ Coding/3C/information_extraction
mkdir -p "$NEW"/{app,tests/fixtures,docs/superpowers/specs,docs/superpowers/plans,scripts}
cd "$NEW" && git init
cp "$SRC/docs/superpowers/specs/2026-07-06-fahrzeugausweis-extraction-design.md" docs/superpowers/specs/
cp "$SRC/docs/superpowers/plans/2026-07-06-garagenhub-extractor.md" docs/superpowers/plans/
cp "$SRC/garagenhub_input/Fahrzeugausweis.jpeg" tests/fixtures/
cp "$SRC/garagenhub_input/Fahrzeugausweis.jpg" tests/fixtures/
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
.env
.pytest_cache/
*.egg-info/
.DS_Store
```

- [ ] **Step 3: Write `requirements.txt`**

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
openai==2.8.0
pydantic==2.10.4
pydantic-settings==2.7.1
python-multipart==0.0.20
Pillow==11.1.0
pillow-heif==0.21.0
pypdfium2==4.30.1
tenacity==9.0.0
structlog==24.4.0
sentry-sdk==2.19.2
```

- [ ] **Step 4: Write `requirements-dev.txt`**

```
-r requirements.txt
pytest==8.3.4
pytest-mock==3.14.0
httpx==0.28.1
python-dotenv==1.0.1
```

- [ ] **Step 5: Write `.env.example`**

```
AZURE_OPENAI_KEY=
AZURE_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-02-15-preview
OPENAI_VISION_MODEL=gpt-5.4
EXTRACTOR_API_KEY=change-me
MAX_UPLOAD_MB=15
REQUEST_TIMEOUT_S=30
MAX_IMAGE_SIDE=1600
SENTRY_DSN=
```

- [ ] **Step 6: Write `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -q
```

- [ ] **Step 7: Write a minimal `README.md`**

```markdown
# garagenhub-extractor

Synchronous API that extracts keeper + vehicle fields from a photo of a Swiss
Fahrzeugausweis in a single vision call. See
`docs/superpowers/specs/2026-07-06-fahrzeugausweis-extraction-design.md`.

## Local dev
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt
    cp .env.example .env   # fill in Azure creds + EXTRACTOR_API_KEY
    uvicorn app.main:app --reload --port 8080

## Test
    pytest
```

- [ ] **Step 8: Create empty package files and write the smoke test**

Create empty `app/__init__.py` and `tests/__init__.py`. Then `tests/test_smoke.py`:

```python
def test_python_and_imports():
    import fastapi  # noqa: F401
    import openai  # noqa: F401
    assert True
```

- [ ] **Step 9: Create venv, install deps, run the smoke test**

Run:
```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
pytest tests/test_smoke.py -v
```
Expected: PASS (1 passed).

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "chore: scaffold garagenhub-extractor repo with design docs and fixtures"
```

---

### Task 2: Settings / config

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `class Settings(BaseSettings)` with fields: `azure_openai_key: str`, `azure_endpoint: str`, `azure_openai_api_version: str = "2024-02-15-preview"`, `openai_vision_model: str = "gpt-5.4"`, `extractor_api_key: str`, `max_upload_mb: int = 15`, `request_timeout_s: int = 30`, `max_image_side: int = 1600`, `sentry_dsn: str | None = None`.
  - `get_settings() -> Settings` (cached).

- [ ] **Step 1: Write the failing test**

```python
import importlib


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://e/")
    monkeypatch.setenv("EXTRACTOR_API_KEY", "secret")
    from app import config
    importlib.reload(config)
    s = config.get_settings()
    assert s.azure_openai_key == "k"
    assert s.openai_vision_model == "gpt-5.4"   # default
    assert s.max_image_side == 1600             # default


def test_settings_cached(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://e/")
    monkeypatch.setenv("EXTRACTOR_API_KEY", "secret")
    from app import config
    importlib.reload(config)
    assert config.get_settings() is config.get_settings()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL (ModuleNotFoundError: app.config).

- [ ] **Step 3: Write `app/config.py`**

```python
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    azure_openai_key: str
    azure_endpoint: str
    azure_openai_api_version: str = "2024-02-15-preview"
    openai_vision_model: str = "gpt-5.4"

    extractor_api_key: str

    max_upload_mb: int = 15
    request_timeout_s: int = 30
    max_image_side: int = 1600

    sentry_dsn: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: add settings/config module"
```

---

### Task 3: Field paths + extraction prompt

**Files:**
- Create: `app/prompt.py`
- Test: `tests/test_prompt.py`

**Interfaces:**
- Produces:
  - `FIELD_PATHS: list[str]` — authoritative dotted paths, e.g. `"halter.vorname"`, `"fahrzeug.vin"`. Exactly the 8 halter fields + 17 fahrzeug fields listed below.
  - `NUMERIC_FIELDS: set[str]` — subset of fahrzeug field names to coerce to numbers: `{"hubraum_cm3", "leistung_kw", "leergewicht_kg", "gesamtgewicht_kg", "plaetze"}`.
  - `build_extract_prompt() -> str`.

- [ ] **Step 1: Write the failing test**

```python
from app.prompt import FIELD_PATHS, NUMERIC_FIELDS, build_extract_prompt


def test_field_paths_complete():
    assert "halter.vorname" in FIELD_PATHS
    assert "halter.nachname" in FIELD_PATHS
    assert "fahrzeug.vin" in FIELD_PATHS
    assert "fahrzeug.kontrollschildfarbe" in FIELD_PATHS
    # 8 halter + 17 fahrzeug
    assert sum(p.startswith("halter.") for p in FIELD_PATHS) == 8
    assert sum(p.startswith("fahrzeug.") for p in FIELD_PATHS) == 17


def test_numeric_fields():
    assert NUMERIC_FIELDS == {
        "hubraum_cm3", "leistung_kw", "leergewicht_kg", "gesamtgewicht_kg", "plaetze"
    }


def test_prompt_mentions_key_rules():
    p = build_extract_prompt()
    assert "Fahrzeugausweis" in p
    assert "vorname" in p and "nachname" in p       # name split
    assert "marke" in p and "modell" in p           # make/model split
    assert "hausnummer" in p                          # street/number split
    assert "confidence" in p                          # asks for confidence map
    assert "JSON" in p
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompt.py -v`
Expected: FAIL (ModuleNotFoundError: app.prompt).

- [ ] **Step 3: Write `app/prompt.py`**

```python
HALTER_FIELDS = [
    "vorname", "nachname", "strasse", "hausnummer",
    "plz", "ort", "versicherung", "geburtsdatum",
]

FAHRZEUG_FIELDS = [
    "kontrollschild", "kontrollschildfarbe", "fahrzeugart", "marke", "modell",
    "vin", "karosserie", "farbe", "stammnummer", "typengenehmigung",
    "hubraum_cm3", "leistung_kw", "leergewicht_kg", "gesamtgewicht_kg",
    "erste_inverkehrsetzung", "emissionscode", "plaetze",
]

FIELD_PATHS = (
    [f"halter.{f}" for f in HALTER_FIELDS]
    + [f"fahrzeug.{f}" for f in FAHRZEUG_FIELDS]
)

NUMERIC_FIELDS = {
    "hubraum_cm3", "leistung_kw", "leergewicht_kg", "gesamtgewicht_kg", "plaetze"
}


def build_extract_prompt() -> str:
    return """Du erhältst das Foto eines Schweizer Fahrzeugausweises (permis de circulation).
Extrahiere die folgenden Felder und gib AUSSCHLIESSLICH ein JSON-Objekt zurück (kein Markdown).
Wenn ein Feld nicht lesbar oder nicht vorhanden ist, gib null.

Struktur:
{
  "halter": {
    "vorname": ...,        // Vorname der Person (Feld "Name, Vornamen", z.B. "Christian")
    "nachname": ...,       // Nachname/Familienname (z.B. "Körner"); auf CH-Ausweisen steht der Nachname meist zuerst
    "strasse": ...,        // nur der Strassenname ohne Hausnummer
    "hausnummer": ...,     // nur die Hausnummer (z.B. "20a")
    "plz": ...,
    "ort": ...,
    "versicherung": ...,   // Feld 09
    "geburtsdatum": ...    // Format TT.MM.JJJJ, Feld 07
  },
  "fahrzeug": {
    "kontrollschild": ...,        // Feld 15, z.B. "SZ 41719"
    "kontrollschildfarbe": ...,   // steht oben rechts, z.B. "weiss"
    "fahrzeugart": ...,           // Feld 19, z.B. "Personenwagen"
    "marke": ...,                 // Hersteller aus Feld 21, z.B. "VW"
    "modell": ...,                // Modell aus Feld 21, z.B. "Golf"
    "vin": ...,                   // Fahrgestell-Nr., Feld 23
    "karosserie": ...,            // Feld 25
    "farbe": ...,                 // Feld 26
    "stammnummer": ...,           // Feld 18
    "typengenehmigung": ...,      // Feld 24
    "hubraum_cm3": ...,           // Zahl, Feld 37
    "leistung_kw": ...,           // Zahl, Feld 76
    "leergewicht_kg": ...,        // Zahl, Feld 30
    "gesamtgewicht_kg": ...,      // Zahl, Feld 33
    "erste_inverkehrsetzung": ..., // Format TT.MM.JJJJ, Feld 36
    "emissionscode": ...,         // Feld 72
    "plaetze": ...                // Zahl, Feld 27
  },
  "interne_hinweise": ...,   // Freitext aus "Kantonale Vermerke / Verfügungen der Behörde" (unten links), sonst null
  "confidence": {
    // Für jedes Feld ein Vertrauensniveau "high" | "medium" | "low",
    // mit gepunktetem Pfad als Schlüssel, z.B. "fahrzeug.vin": "high".
    // Nur Felder aufnehmen, die du nicht mit hoher Sicherheit gelesen hast (medium/low).
  }
}

Regeln:
- Zahlenfelder als Zahl ohne Einheit und ohne führende Sternchen (z.B. "**1630" -> 1630).
- Datumsfelder im Format TT.MM.JJJJ.
- Trenne Name in vorname/nachname, "Marke und Typ" in marke/modell, Adresse in strasse/hausnummer."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompt.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/prompt.py tests/test_prompt.py
git commit -m "feat: add field paths and extraction prompt"
```

---

### Task 4: Response models

**Files:**
- Create: `app/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces Pydantic models:
  - `Halter` — 8 optional str fields (all default `None`): `vorname, nachname, strasse, hausnummer, plz, ort, versicherung, geburtsdatum`.
  - `Fahrzeug` — string fields `kontrollschild, kontrollschildfarbe, fahrzeugart, marke, modell, vin, karosserie, farbe, stammnummer, typengenehmigung, erste_inverkehrsetzung, emissionscode` (all `str | None = None`); numeric fields `hubraum_cm3, leistung_kw, leergewicht_kg, gesamtgewicht_kg` (`float | int | None = None`) and `plaetze` (`int | None = None`).
  - `FieldWarning` — `field: str`, `reason: str`.
  - `Meta` — `model: str`, `processing_ms: int`, `image_pages: int`.
  - `ExtractionResponse` — `halter: Halter`, `fahrzeug: Fahrzeug`, `interne_hinweise: str | None = None`, `warnings: list[FieldWarning] = []`, `field_confidence: dict[str, str] = {}`, `meta: Meta`.

- [ ] **Step 1: Write the failing test**

```python
from app.models import ExtractionResponse, Halter, Fahrzeug, Meta, FieldWarning


def test_models_construct_with_defaults():
    r = ExtractionResponse(
        halter=Halter(vorname="Christian", nachname="Körner"),
        fahrzeug=Fahrzeug(vin="WVWZZZ...", hubraum_cm3=1984, plaetze=5),
        meta=Meta(model="gpt-5.4", processing_ms=5000, image_pages=1),
    )
    assert r.halter.plz is None
    assert r.fahrzeug.hubraum_cm3 == 1984
    assert r.warnings == []
    assert r.field_confidence == {}


def test_numeric_coercion_via_pydantic():
    f = Fahrzeug(leistung_kw=235.0, plaetze=5)
    assert f.leistung_kw == 235.0
    assert f.plaetze == 5


def test_warning_shape():
    w = FieldWarning(field="fahrzeug.vin", reason="format_invalid")
    assert w.field == "fahrzeug.vin"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL (ModuleNotFoundError: app.models).

- [ ] **Step 3: Write `app/models.py`**

```python
from pydantic import BaseModel, Field


class Halter(BaseModel):
    vorname: str | None = None
    nachname: str | None = None
    strasse: str | None = None
    hausnummer: str | None = None
    plz: str | None = None
    ort: str | None = None
    versicherung: str | None = None
    geburtsdatum: str | None = None


class Fahrzeug(BaseModel):
    kontrollschild: str | None = None
    kontrollschildfarbe: str | None = None
    fahrzeugart: str | None = None
    marke: str | None = None
    modell: str | None = None
    vin: str | None = None
    karosserie: str | None = None
    farbe: str | None = None
    stammnummer: str | None = None
    typengenehmigung: str | None = None
    hubraum_cm3: float | int | None = None
    leistung_kw: float | int | None = None
    leergewicht_kg: float | int | None = None
    gesamtgewicht_kg: float | int | None = None
    erste_inverkehrsetzung: str | None = None
    emissionscode: str | None = None
    plaetze: int | None = None


class FieldWarning(BaseModel):
    field: str
    reason: str


class Meta(BaseModel):
    model: str
    processing_ms: int
    image_pages: int


class ExtractionResponse(BaseModel):
    halter: Halter
    fahrzeug: Fahrzeug
    interne_hinweise: str | None = None
    warnings: list[FieldWarning] = Field(default_factory=list)
    field_confidence: dict[str, str] = Field(default_factory=dict)
    meta: Meta
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/models.py tests/test_models.py
git commit -m "feat: add pydantic response models"
```

---

### Task 5: Validation helpers

**Files:**
- Create: `app/validation.py`
- Test: `tests/test_validation.py`

**Interfaces:**
- Produces:
  - `normalize_vin(v: str | None) -> str | None` — strip whitespace, uppercase.
  - `is_valid_vin(v: str | None) -> bool` — normalized length 17, charset `[A-HJ-NPR-Z0-9]`.
  - `is_valid_kontrollschild(v: str | None) -> bool` — pattern `^[A-Z]{2}\s?\d{1,6}$`.
  - `is_valid_date(v: str | None) -> bool` — parses as `%d.%m.%Y`.
  - `coerce_number(v) -> int | float | None` — strip leading `*`, spaces, comma→dot; int if integral else float; None if unparseable.
  - `collect_warnings(halter: dict, fahrzeug: dict) -> list[dict]` — returns `[{"field": ..., "reason": ...}]` for present-but-invalid fields (vin/kontrollschild → `format_invalid`; geburtsdatum/erste_inverkehrsetzung → `date_invalid`). Skips `None`/empty values (missing ≠ invalid).

- [ ] **Step 1: Write the failing test**

```python
from app.validation import (
    normalize_vin, is_valid_vin, is_valid_kontrollschild,
    is_valid_date, coerce_number, collect_warnings,
)


def test_vin():
    assert is_valid_vin("WVW ZZZ CDZ NW1 321 44") is True   # 17 after strip
    assert is_valid_vin("VF1 RFC 000 588 765 20") is True
    assert is_valid_vin("SHORT") is False
    assert is_valid_vin(None) is False
    assert normalize_vin("wvw zzz") == "WVWZZZ"


def test_kontrollschild():
    assert is_valid_kontrollschild("SZ 41719") is True
    assert is_valid_kontrollschild("ZH123456") is True
    assert is_valid_kontrollschild("banana") is False
    assert is_valid_kontrollschild(None) is False


def test_date():
    assert is_valid_date("07.02.2022") is True
    assert is_valid_date("2022-02-07") is False
    assert is_valid_date(None) is False


def test_coerce_number():
    assert coerce_number("**1630") == 1630
    assert coerce_number("235.0") == 235.0
    assert coerce_number("*118.0") == 118.0
    assert coerce_number("1 984") == 1984
    assert coerce_number("") is None
    assert coerce_number(None) is None
    assert coerce_number("n/a") is None
    assert coerce_number(5) == 5


def test_collect_warnings():
    halter = {"geburtsdatum": "notadate"}
    fahrzeug = {"vin": "SHORT", "kontrollschild": "SZ 41719"}
    warns = collect_warnings(halter, fahrzeug)
    fields = {w["field"]: w["reason"] for w in warns}
    assert fields["fahrzeug.vin"] == "format_invalid"
    assert fields["halter.geburtsdatum"] == "date_invalid"
    assert "fahrzeug.kontrollschild" not in fields   # valid, no warning


def test_collect_warnings_skips_missing():
    assert collect_warnings({}, {"vin": None}) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_validation.py -v`
Expected: FAIL (ModuleNotFoundError: app.validation).

- [ ] **Step 3: Write `app/validation.py`**

```python
import re
from datetime import datetime

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_PLATE_RE = re.compile(r"^[A-Z]{2}\s?\d{1,6}$")


def normalize_vin(v: str | None) -> str | None:
    if v is None:
        return None
    return re.sub(r"\s+", "", v).upper()


def is_valid_vin(v: str | None) -> bool:
    n = normalize_vin(v)
    return bool(n) and _VIN_RE.fullmatch(n) is not None


def is_valid_kontrollschild(v: str | None) -> bool:
    if not v:
        return False
    return _PLATE_RE.fullmatch(v.strip().upper()) is not None


def is_valid_date(v: str | None) -> bool:
    if not v:
        return False
    try:
        datetime.strptime(v.strip(), "%d.%m.%Y")
        return True
    except ValueError:
        return False


def coerce_number(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else v
    s = str(v).strip().lstrip("*").replace(" ", "").replace(",", ".")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def collect_warnings(halter: dict, fahrzeug: dict) -> list[dict]:
    warnings: list[dict] = []

    vin = fahrzeug.get("vin")
    if vin and not is_valid_vin(vin):
        warnings.append({"field": "fahrzeug.vin", "reason": "format_invalid"})

    plate = fahrzeug.get("kontrollschild")
    if plate and not is_valid_kontrollschild(plate):
        warnings.append({"field": "fahrzeug.kontrollschild", "reason": "format_invalid"})

    ez = fahrzeug.get("erste_inverkehrsetzung")
    if ez and not is_valid_date(ez):
        warnings.append({"field": "fahrzeug.erste_inverkehrsetzung", "reason": "date_invalid"})

    gd = halter.get("geburtsdatum")
    if gd and not is_valid_date(gd):
        warnings.append({"field": "halter.geburtsdatum", "reason": "date_invalid"})

    return warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_validation.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add app/validation.py tests/test_validation.py
git commit -m "feat: add deterministic field validation helpers"
```

---

### Task 6: Image preprocessing

**Files:**
- Create: `app/preprocessing.py`
- Test: `tests/test_preprocessing.py`

**Interfaces:**
- Consumes: `Settings.max_image_side` (passed as int arg, not the whole Settings object).
- Produces:
  - `class UnsupportedImage(Exception)` — raised on undecodable/empty/unsupported input.
  - `@dataclass NormalizedImage` — `b64: str`, `mime: str = "image/jpeg"`, `page_count: int = 1`.
  - `normalize_image(data: bytes, *, filename: str | None = None, content_type: str | None = None, max_side: int = 1600) -> NormalizedImage` — detects PDF via `%PDF` magic bytes (renders first page via pypdfium2, `page_count` = total pages), else opens via Pillow (pillow-heif registered for HEIC), converts to RGB, downscales so longest side ≤ `max_side`, re-encodes JPEG q90, base64.

- [ ] **Step 1: Write the failing test**

```python
import base64
import io

import pypdfium2 as pdfium
import pytest
from PIL import Image

from app.preprocessing import normalize_image, NormalizedImage, UnsupportedImage


def _jpeg_bytes(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 120, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 200, 10)).save(buf, format="PNG")
    return buf.getvalue()


def _pdf_bytes(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 10, 10)).save(buf, format="PDF")
    return buf.getvalue()


def _decode_side(norm: NormalizedImage):
    img = Image.open(io.BytesIO(base64.b64decode(norm.b64)))
    return max(img.size)


def test_jpeg_downscaled():
    norm = normalize_image(_jpeg_bytes(4000, 3000), content_type="image/jpeg", max_side=1600)
    assert norm.mime == "image/jpeg"
    assert norm.page_count == 1
    assert _decode_side(norm) == 1600


def test_png_accepted_and_converted_to_jpeg():
    norm = normalize_image(_png_bytes(800, 600), content_type="image/png", max_side=1600)
    assert norm.mime == "image/jpeg"
    assert _decode_side(norm) == 800   # small image not upscaled


def test_pdf_first_page():
    norm = normalize_image(_pdf_bytes(2000, 1500), content_type="application/pdf", max_side=1600)
    assert norm.page_count >= 1
    assert _decode_side(norm) == 1600


def test_empty_raises():
    with pytest.raises(UnsupportedImage):
        normalize_image(b"", content_type="image/jpeg")


def test_garbage_raises():
    with pytest.raises(UnsupportedImage):
        normalize_image(b"not an image", content_type="image/jpeg")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_preprocessing.py -v`
Expected: FAIL (ModuleNotFoundError: app.preprocessing).

- [ ] **Step 3: Write `app/preprocessing.py`**

```python
import base64
import io
from dataclasses import dataclass

import pypdfium2 as pdfium
from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional decoder
    pass


class UnsupportedImage(Exception):
    """Raised when the uploaded bytes cannot be decoded into an image."""


@dataclass
class NormalizedImage:
    b64: str
    mime: str = "image/jpeg"
    page_count: int = 1


def _downscale(img: Image.Image, max_side: int) -> Image.Image:
    longest = max(img.size)
    if longest > max_side:
        ratio = max_side / longest
        img = img.resize((round(img.width * ratio), round(img.height * ratio)))
    return img


def _render_pdf_first_page(data: bytes, max_side: int) -> tuple[Image.Image, int]:
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedImage(f"unreadable PDF: {exc}") from exc
    try:
        page_count = len(pdf)
        if page_count == 0:
            raise UnsupportedImage("empty PDF")
        # scale up so the rendered longest side is ~max_side before downscale
        page = pdf[0]
        scale = max_side / max(page.get_size())
        bitmap = page.render(scale=max(scale, 1.0))
        img = bitmap.to_pil().convert("RGB")
        return img, page_count
    finally:
        pdf.close()


def normalize_image(
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    max_side: int = 1600,
) -> NormalizedImage:
    if not data:
        raise UnsupportedImage("empty upload")

    is_pdf = data[:5] == b"%PDF-" or (content_type or "").endswith("pdf")
    if is_pdf:
        img, page_count = _render_pdf_first_page(data, max_side)
    else:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            img.load()
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedImage(f"undecodable image: {exc}") from exc
        page_count = 1

    img = _downscale(img, max_side)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return NormalizedImage(b64=b64, mime="image/jpeg", page_count=page_count)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_preprocessing.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add app/preprocessing.py tests/test_preprocessing.py
git commit -m "feat: add image preprocessing (downscale, HEIC/PDF, base64)"
```

---

### Task 7: Extractor (Azure OpenAI vision call + parse)

**Files:**
- Create: `app/extractor.py`
- Test: `tests/test_extractor.py`

**Interfaces:**
- Consumes: `NormalizedImage` (Task 6), `build_extract_prompt` (Task 3).
- Produces:
  - `class ExtractionError(Exception)`.
  - `build_content_blocks(normalized: NormalizedImage) -> list[dict]` — `[{"type":"text","text":prompt}, {"type":"image_url","image_url":{"url": f"data:{mime};base64,{b64}", "detail":"high"}}]`.
  - `parse_response(text: str) -> dict` — strips ```` ```json ```` / ```` ``` ```` fences, `json.loads`; raises `ExtractionError` on failure.
  - `extract_fields(normalized, *, client, model, reparse=True) -> tuple[dict, dict]` — returns `(raw_dict, usage)` where `usage = {"prompt_tokens": int, "completion_tokens": int}`. Calls `_call_openai` (tenacity 3×). On parse failure, retries the whole call once if `reparse`, then raises.
  - `_call_openai(client, model, content_blocks)` — `client.chat.completions.create(model=model, messages=[{"role":"user","content":content_blocks}], temperature=0, seed=42)`, wrapped in tenacity `stop_after_attempt(3), wait_exponential(multiplier=1, min=2, max=30)`.

- [ ] **Step 1: Write the failing test**

```python
import json
from types import SimpleNamespace

import pytest

from app.extractor import (
    build_content_blocks, parse_response, extract_fields, ExtractionError,
)
from app.preprocessing import NormalizedImage


def _fake_response(content: str, pt=1000, ct=300):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct),
    )


class _FakeClient:
    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        return _fake_response(self._contents.pop(0))


def test_build_content_blocks():
    norm = NormalizedImage(b64="QUJD", mime="image/jpeg")
    blocks = build_content_blocks(norm)
    assert blocks[0]["type"] == "text"
    assert blocks[1]["image_url"]["detail"] == "high"
    assert blocks[1]["image_url"]["url"].startswith("data:image/jpeg;base64,QUJD")


def test_parse_response_strips_fences():
    d = parse_response('```json\n{"a": 1}\n```')
    assert d == {"a": 1}


def test_parse_response_raises_on_garbage():
    with pytest.raises(ExtractionError):
        parse_response("not json at all")


def test_extract_fields_happy():
    payload = json.dumps({"halter": {"vorname": "Christian"}, "fahrzeug": {}})
    client = _FakeClient([payload])
    raw, usage = extract_fields(NormalizedImage(b64="QUJD"), client=client, model="gpt-5.4")
    assert raw["halter"]["vorname"] == "Christian"
    assert usage["prompt_tokens"] == 1000
    assert client.calls == 1


def test_extract_fields_reparses_once():
    good = json.dumps({"halter": {}, "fahrzeug": {}})
    client = _FakeClient(["garbage", good])
    raw, _ = extract_fields(NormalizedImage(b64="QUJD"), client=client, model="gpt-5.4")
    assert client.calls == 2
    assert raw == {"halter": {}, "fahrzeug": {}}


def test_extract_fields_raises_after_reparse():
    client = _FakeClient(["garbage", "still garbage"])
    with pytest.raises(ExtractionError):
        extract_fields(NormalizedImage(b64="QUJD"), client=client, model="gpt-5.4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_extractor.py -v`
Expected: FAIL (ModuleNotFoundError: app.extractor).

- [ ] **Step 3: Write `app/extractor.py`**

```python
import json

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from app.preprocessing import NormalizedImage
from app.prompt import build_extract_prompt

log = structlog.get_logger()


class ExtractionError(Exception):
    """Raised when the model output cannot be parsed as the expected JSON."""


def build_content_blocks(normalized: NormalizedImage) -> list[dict]:
    return [
        {"type": "text", "text": build_extract_prompt()},
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{normalized.mime};base64,{normalized.b64}",
                "detail": "high",
            },
        },
    ]


def parse_response(text: str) -> dict:
    if not text:
        raise ExtractionError("empty model response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model returned non-JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError("model JSON is not an object")
    return data


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30), reraise=True)
def _call_openai(client, model, content_blocks):
    return client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content_blocks}],
        temperature=0,
        seed=42,
    )


def extract_fields(normalized: NormalizedImage, *, client, model, reparse: bool = True):
    blocks = build_content_blocks(normalized)
    attempts = 2 if reparse else 1
    last_exc: Exception | None = None
    for _ in range(attempts):
        resp = _call_openai(client, model, blocks)
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        }
        try:
            raw = parse_response(resp.choices[0].message.content)
            return raw, usage
        except ExtractionError as exc:
            last_exc = exc
            log.warning("extract_parse_failed", error=str(exc))
    raise last_exc  # type: ignore[misc]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_extractor.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add app/extractor.py tests/test_extractor.py
git commit -m "feat: add azure vision extractor with parse + reparse retry"
```

---

### Task 8: Assembler (raw dict → validated ExtractionResponse)

**Files:**
- Create: `app/assembler.py`
- Test: `tests/test_assembler.py`

**Interfaces:**
- Consumes: `NUMERIC_FIELDS` (Task 3), `coerce_number` + `collect_warnings` (Task 5), all models (Task 4).
- Produces:
  - `build_response(raw: dict, *, model: str, processing_ms: int, image_pages: int) -> ExtractionResponse` — pulls `raw["halter"]` / `raw["fahrzeug"]` (default `{}`), coerces `NUMERIC_FIELDS` on fahrzeug via `coerce_number`, collects warnings from the coerced dicts, filters `raw["confidence"]` to values in `{"high","medium","low"}` (lowercased), builds and returns `ExtractionResponse`.

- [ ] **Step 1: Write the failing test**

```python
from app.assembler import build_response


def test_build_response_coerces_and_validates():
    raw = {
        "halter": {"vorname": "Christian", "nachname": "Körner", "geburtsdatum": "08.05.1978"},
        "fahrzeug": {
            "vin": "SHORT", "kontrollschild": "SZ 41719",
            "hubraum_cm3": "**1984", "leistung_kw": "235.0", "plaetze": "5",
        },
        "interne_hinweise": "178 ...",
        "confidence": {"fahrzeug.vin": "LOW", "fahrzeug.bogus": "banana"},
    }
    resp = build_response(raw, model="gpt-5.4", processing_ms=4200, image_pages=1)

    assert resp.halter.vorname == "Christian"
    assert resp.fahrzeug.hubraum_cm3 == 1984      # coerced int, asterisks stripped
    assert resp.fahrzeug.leistung_kw == 235.0
    assert resp.fahrzeug.plaetze == 5
    # invalid VIN -> warning
    assert any(w.field == "fahrzeug.vin" and w.reason == "format_invalid" for w in resp.warnings)
    # confidence normalized + filtered
    assert resp.field_confidence["fahrzeug.vin"] == "low"
    assert "fahrzeug.bogus" not in resp.field_confidence
    assert resp.meta.processing_ms == 4200
    assert resp.meta.image_pages == 1


def test_build_response_handles_missing_sections():
    resp = build_response({}, model="gpt-5.4", processing_ms=1, image_pages=1)
    assert resp.halter.vorname is None
    assert resp.fahrzeug.vin is None
    assert resp.warnings == []
    assert resp.field_confidence == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_assembler.py -v`
Expected: FAIL (ModuleNotFoundError: app.assembler).

- [ ] **Step 3: Write `app/assembler.py`**

```python
from app.models import ExtractionResponse, Fahrzeug, Halter, Meta
from app.prompt import NUMERIC_FIELDS
from app.validation import coerce_number, collect_warnings

_CONFIDENCE_LEVELS = {"high", "medium", "low"}


def build_response(raw: dict, *, model: str, processing_ms: int, image_pages: int) -> ExtractionResponse:
    halter = dict(raw.get("halter") or {})
    fahrzeug = dict(raw.get("fahrzeug") or {})

    for field in NUMERIC_FIELDS:
        if field in fahrzeug:
            fahrzeug[field] = coerce_number(fahrzeug[field])

    warnings = collect_warnings(halter, fahrzeug)

    confidence = {}
    for key, value in (raw.get("confidence") or {}).items():
        level = str(value).strip().lower()
        if level in _CONFIDENCE_LEVELS:
            confidence[key] = level

    return ExtractionResponse(
        halter=Halter(**{k: halter.get(k) for k in Halter.model_fields}),
        fahrzeug=Fahrzeug(**{k: fahrzeug.get(k) for k in Fahrzeug.model_fields}),
        interne_hinweise=raw.get("interne_hinweise"),
        warnings=warnings,
        field_confidence=confidence,
        meta=Meta(model=model, processing_ms=processing_ms, image_pages=image_pages),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_assembler.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add app/assembler.py tests/test_assembler.py
git commit -m "feat: add assembler from raw dict to validated response"
```

---

### Task 9: FastAPI app (auth, /extract, /healthz)

**Files:**
- Create: `app/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - FastAPI `app`.
  - `require_api_key(x_api_key: str = Header(None))` dependency → 401 if mismatch.
  - `get_client()` → cached `AzureOpenAI` client built from settings.
  - `GET /healthz` → `{"status": "ok"}` (no auth).
  - `POST /extract` (auth) → accepts `file: UploadFile`, enforces `max_upload_mb`, normalizes, extracts, assembles, returns `ExtractionResponse`. `400` on `UnsupportedImage`/oversize, `502` on `ExtractionError`/upstream failure.

- [ ] **Step 1: Write the failing test**

```python
import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_KEY", "k")
    monkeypatch.setenv("AZURE_ENDPOINT", "https://e/")
    monkeypatch.setenv("EXTRACTOR_API_KEY", "secret")
    import importlib
    from app import config, main
    importlib.reload(config)
    importlib.reload(main)

    # stub the Azure client + extract call so no network happens
    payload = json.dumps({
        "halter": {"vorname": "Christian", "nachname": "Körner"},
        "fahrzeug": {"kontrollschild": "SZ 41719", "vin": "WVW ZZZ CDZ NW1 321 44"},
        "interne_hinweise": None,
        "confidence": {},
    })

    def fake_extract(normalized, *, client, model, reparse=True):
        return json.loads(payload), {"prompt_tokens": 1000, "completion_tokens": 300}

    monkeypatch.setattr(main, "extract_fields", fake_extract)
    monkeypatch.setattr(main, "get_client", lambda: object())
    return TestClient(main.app)


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (120, 120, 120)).save(buf, format="JPEG")
    buf.seek(0)
    return buf


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_extract_requires_key(client):
    r = client.post("/extract", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 401


def test_extract_happy(client):
    r = client.post(
        "/extract",
        headers={"X-Api-Key": "secret"},
        files={"file": ("a.jpg", _jpeg(), "image/jpeg")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["halter"]["nachname"] == "Körner"
    assert body["fahrzeug"]["kontrollschild"] == "SZ 41719"
    assert body["meta"]["model"] == "gpt-5.4"


def test_extract_bad_file(client):
    r = client.post(
        "/extract",
        headers={"X-Api-Key": "secret"},
        files={"file": ("a.jpg", io.BytesIO(b"not an image"), "image/jpeg")},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -v`
Expected: FAIL (ModuleNotFoundError: app.main).

- [ ] **Step 3: Write `app/main.py`**

```python
import time
from functools import lru_cache

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile
from openai import AzureOpenAI

from app.assembler import build_response
from app.config import get_settings
from app.extractor import ExtractionError, extract_fields
from app.preprocessing import UnsupportedImage, normalize_image

log = structlog.get_logger()
app = FastAPI(title="GaragenHub Fahrzeugausweis Extractor")


def require_api_key(x_api_key: str = Header(None)) -> None:
    if x_api_key != get_settings().extractor_api_key:
        raise HTTPException(status_code=401, detail="invalid or missing X-Api-Key")


@lru_cache
def get_client() -> AzureOpenAI:
    s = get_settings()
    return AzureOpenAI(
        api_key=s.azure_openai_key,
        azure_endpoint=s.azure_endpoint,
        api_version=s.azure_openai_api_version,
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/extract", dependencies=[Depends(require_api_key)])
async def extract(file: UploadFile):
    settings = get_settings()
    data = await file.read()

    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"file exceeds {settings.max_upload_mb} MB")

    try:
        normalized = normalize_image(
            data,
            filename=file.filename,
            content_type=file.content_type,
            max_side=settings.max_image_side,
        )
    except UnsupportedImage as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    t0 = time.perf_counter()
    try:
        raw, usage = extract_fields(
            normalized, client=get_client(), model=settings.openai_vision_model
        )
    except ExtractionError as exc:
        raise HTTPException(status_code=502, detail="extraction failed") from exc
    except Exception as exc:  # noqa: BLE001 - upstream/openai failure
        log.error("upstream_error", error=str(exc))
        raise HTTPException(status_code=502, detail="upstream model error") from exc

    processing_ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "extracted",
        processing_ms=processing_ms,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        pages=normalized.page_count,
    )
    return build_response(
        raw,
        model=settings.openai_vision_model,
        processing_ms=processing_ms,
        image_pages=normalized.page_count,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_api.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: PASS (all tasks' tests green).

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat: add FastAPI app with /extract and /healthz"
```

---

### Task 10: Local CLI + opt-in integration test over the real cards

**Files:**
- Create: `scripts/extract_local.py`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: `normalize_image`, `extract_fields`, `build_response`, `get_client`, `get_settings`.
- Produces: a CLI `python scripts/extract_local.py <image>` that prints the JSON response; an integration test that hits real Azure over the two fixture cards, **skipped unless `AZURE_OPENAI_KEY` is set**.

- [ ] **Step 1: Write the opt-in integration test**

```python
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("AZURE_OPENAI_KEY"), reason="no Azure creds; integration test skipped"
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.mark.parametrize(
    "name,plate,marke",
    [("Fahrzeugausweis.jpeg", "SZ 41719", "VW"), ("Fahrzeugausweis.jpg", "SZ 96382", "RENAULT")],
)
def test_real_card(name, plate, marke):
    from app.config import get_settings
    from app.preprocessing import normalize_image
    from app.extractor import extract_fields
    from app.assembler import build_response
    from app.main import get_client

    with open(os.path.join(FIXTURES, name), "rb") as fh:
        data = fh.read()
    s = get_settings()
    norm = normalize_image(data, content_type="image/jpeg", max_side=s.max_image_side)
    raw, _ = extract_fields(norm, client=get_client(), model=s.openai_vision_model)
    resp = build_response(raw, model=s.openai_vision_model, processing_ms=0, image_pages=1)

    assert resp.fahrzeug.kontrollschild == plate
    assert resp.fahrzeug.marke.upper().startswith(marke)
    assert resp.fahrzeug.kontrollschildfarbe.lower() == "weiss"
```

- [ ] **Step 2: Write `scripts/extract_local.py`**

```python
"""CLI: python scripts/extract_local.py <image-path>  -> prints JSON."""
import json
import sys

from app.assembler import build_response
from app.config import get_settings
from app.extractor import extract_fields
from app.main import get_client
from app.preprocessing import normalize_image


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/extract_local.py <image-path>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    with open(path, "rb") as fh:
        data = fh.read()
    s = get_settings()
    norm = normalize_image(data, filename=path, max_side=s.max_image_side)
    raw, usage = extract_fields(norm, client=get_client(), model=s.openai_vision_model)
    resp = build_response(raw, model=s.openai_vision_model, processing_ms=0, image_pages=norm.page_count)
    print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))
    print(f"[tokens] in={usage['prompt_tokens']} out={usage['completion_tokens']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Verify the integration test skips cleanly without creds, and run the CLI with real creds**

Run (no creds → skip):
```bash
env -u AZURE_OPENAI_KEY pytest tests/test_integration.py -v
```
Expected: 2 skipped.

Run (with creds, real call — manual check both cards extract correctly):
```bash
python scripts/extract_local.py tests/fixtures/Fahrzeugausweis.jpeg
```
Expected: JSON with `"kontrollschild": "SZ 41719"`, `"marke": "VW"`, `"kontrollschildfarbe": "weiss"`.

- [ ] **Step 4: Commit**

```bash
git add scripts/extract_local.py tests/test_integration.py
git commit -m "feat: add local CLI and opt-in integration test over real cards"
```

---

### Task 11: Containerization + deployment scripts

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `deploy.sh`
- Create: `provision.sh`
- Modify: `README.md` (add deployment section)

**Interfaces:**
- Produces: a runnable container image and scripts to provision + deploy `ca-garagenhub` in the existing Azure environment. No new tests (verified via `docker build`).

- [ ] **Step 1: Write the `Dockerfile`**

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 2: Write `.dockerignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
tests/
docs/
scripts/
.env
.git/
```

- [ ] **Step 3: Write `provision.sh`** (one-time Container App creation)

```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: ./provision.sh <image-tag>
# Requires in env: AZURE_OPENAI_KEY AZURE_ENDPOINT AZURE_OPENAI_API_VERSION EXTRACTOR_API_KEY
IMAGE_TAG="${1:?Usage: ./provision.sh <image-tag>}"
RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
ENV_NAME="cae-3c-invoice"
APP="ca-garagenhub"
IMAGE_REPO="garagenhub-extractor"

acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
echo "==> Building ${IMAGE_REPO}:${IMAGE_TAG} in ACR..."
az acr build --registry "$ACR_NAME" --image "${IMAGE_REPO}:${IMAGE_TAG}" .

if az containerapp show --name "$APP" --resource-group "$RG" >/dev/null 2>&1; then
  echo "==> $APP already exists; use ./deploy.sh to update."
  exit 0
fi

echo "==> Creating $APP (min-replicas 1, always warm)..."
az containerapp create \
  --name "$APP" --resource-group "$RG" --environment "$ENV_NAME" \
  --image "${acr_server}/${IMAGE_REPO}:${IMAGE_TAG}" \
  --target-port 8080 --ingress external \
  --min-replicas 1 --max-replicas 3 \
  --cpu 0.5 --memory 1.0Gi \
  --secrets openai-key="$AZURE_OPENAI_KEY" api-key="$EXTRACTOR_API_KEY" \
  --env-vars \
    AZURE_OPENAI_KEY=secretref:openai-key \
    EXTRACTOR_API_KEY=secretref:api-key \
    AZURE_ENDPOINT="$AZURE_ENDPOINT" \
    AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
    OPENAI_VISION_MODEL=gpt-5.4
echo "==> Done. Endpoint:"
az containerapp show --name "$APP" --resource-group "$RG" --query properties.configuration.ingress.fqdn -o tsv
```

- [ ] **Step 4: Write `deploy.sh`** (update existing app)

```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: ./deploy.sh <image-tag>   (use a UNIQUE tag; "latest" won't create a new revision)
IMAGE_TAG="${1:?Usage: ./deploy.sh <image-tag>}"
RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
APP="ca-garagenhub"
IMAGE_REPO="garagenhub-extractor"

acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
echo "==> Building ${IMAGE_REPO}:${IMAGE_TAG} in ACR..."
az acr build --registry "$ACR_NAME" --image "${IMAGE_REPO}:${IMAGE_TAG}" .
echo "==> Updating $APP..."
az containerapp update --name "$APP" --resource-group "$RG" \
  --image "${acr_server}/${IMAGE_REPO}:${IMAGE_TAG}"
echo "==> Done."
```

- [ ] **Step 5: Make scripts executable and verify the image builds**

Run:
```bash
chmod +x provision.sh deploy.sh
docker build -t garagenhub-extractor:local .
```
Expected: image builds successfully.

- [ ] **Step 6: Append the deployment section to `README.md`**

```markdown
## Deployment (Azure Container Apps)

Reuses ACR `cr3cinvoice` and environment `cae-3c-invoice`. App: `ca-garagenhub`
(API only, `min-replicas 1` so it stays warm for sync latency).

First time:
    export AZURE_OPENAI_KEY=... AZURE_ENDPOINT=... AZURE_OPENAI_API_VERSION=... EXTRACTOR_API_KEY=...
    ./provision.sh v20260706

Subsequent deploys (always use a unique tag; `latest` silently skips new revisions):
    ./deploy.sh v20260707

**Data residency:** open question — the default Azure OpenAI deployment is EU
(Germany West Central). Confirm CH-personal-data handling before go-live; switch
to Switzerland North if required (needs a capacity check first).
```

- [ ] **Step 7: Commit**

```bash
git add Dockerfile .dockerignore provision.sh deploy.sh README.md
git commit -m "feat: add Dockerfile and provision/deploy scripts for ca-garagenhub"
```

---

### Task 12: Streamlit UI (HTTP client of the API)

**Files:**
- Create: `ui/__init__.py` (empty)
- Create: `ui/api_client.py`
- Create: `ui/auth.py`
- Create: `ui/app.py`
- Create: `ui/requirements.txt`
- Modify: `requirements-dev.txt` (add `requests` so client tests can run)
- Test: `tests/test_ui_client.py`
- Test: `tests/test_ui_auth.py`

**Interfaces:**
- Produces:
  - `class ApiError(Exception)` with attributes `status: int`, `detail: str`.
  - `extract(api_url: str, api_key: str, filename: str, data: bytes, content_type: str, timeout: int = 60) -> dict` — POSTs `multipart` `file` to `{api_url}/extract` with header `X-Api-Key`; returns parsed JSON on 200, else raises `ApiError`.
  - `gate_enabled(expected: str | None) -> bool` — True iff a non-empty password is configured.
  - `check_password(entered: str | None, expected: str | None) -> bool` — True if the gate is disabled, else `entered == expected`.
  - `ui/app.py` — Streamlit entrypoint (rendering only; not unit-tested).

- [ ] **Step 1: Add `requests` to `requirements-dev.txt`**

Append this line to `requirements-dev.txt`:

```
requests==2.32.3
```

Then create empty `ui/__init__.py`.

- [ ] **Step 2: Write the failing tests**

`tests/test_ui_auth.py`:

```python
from ui.auth import gate_enabled, check_password


def test_gate_disabled_when_no_password():
    assert gate_enabled(None) is False
    assert gate_enabled("") is False
    assert check_password(None, None) is True     # disabled -> always allowed


def test_gate_enabled_and_checks():
    assert gate_enabled("s3cret") is True
    assert check_password("s3cret", "s3cret") is True
    assert check_password("wrong", "s3cret") is False
    assert check_password(None, "s3cret") is False
```

`tests/test_ui_client.py`:

```python
from types import SimpleNamespace

import pytest

from ui.api_client import extract, ApiError


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_extract_success(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, files=None, timeout=None):
        captured["url"] = url
        captured["key"] = headers["X-Api-Key"]
        return _Resp(200, {"halter": {"vorname": "Christian"}})

    monkeypatch.setattr("ui.api_client.requests.post", fake_post)
    out = extract("http://api", "secret", "a.jpg", b"xx", "image/jpeg")
    assert out["halter"]["vorname"] == "Christian"
    assert captured["url"] == "http://api/extract"
    assert captured["key"] == "secret"


def test_extract_error_raises(monkeypatch):
    monkeypatch.setattr("ui.api_client.requests.post", lambda *a, **k: _Resp(400, {"detail": "bad image"}))
    with pytest.raises(ApiError) as exc:
        extract("http://api", "secret", "a.jpg", b"xx", "image/jpeg")
    assert exc.value.status == 400
    assert "bad image" in exc.value.detail
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_ui_client.py tests/test_ui_auth.py -v`
Expected: FAIL (ModuleNotFoundError: ui.api_client / ui.auth).

- [ ] **Step 4: Write `ui/auth.py`**

```python
def gate_enabled(expected: str | None) -> bool:
    return bool(expected)


def check_password(entered: str | None, expected: str | None) -> bool:
    if not gate_enabled(expected):
        return True
    return entered == expected
```

- [ ] **Step 5: Write `ui/api_client.py`**

```python
import requests


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"[{status}] {detail}")


def extract(
    api_url: str,
    api_key: str,
    filename: str,
    data: bytes,
    content_type: str,
    timeout: int = 60,
) -> dict:
    resp = requests.post(
        f"{api_url.rstrip('/')}/extract",
        headers={"X-Api-Key": api_key},
        files={"file": (filename, data, content_type)},
        timeout=timeout,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:  # noqa: BLE001
            detail = resp.text
        raise ApiError(resp.status_code, str(detail))
    return resp.json()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_ui_client.py tests/test_ui_auth.py -v`
Expected: PASS (4 passed).

- [ ] **Step 7: Write `ui/requirements.txt`**

```
streamlit==1.41.1
requests==2.32.3
```

- [ ] **Step 8: Write `ui/app.py`**

```python
import os
import sys
import time

# Make `ui` importable whether launched via `streamlit run ui/app.py` (root) or in-container.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from ui.api_client import extract, ApiError
from ui.auth import check_password, gate_enabled

API_URL = os.getenv("EXTRACTOR_API_URL", "http://localhost:8080")
API_KEY = os.getenv("EXTRACTOR_API_KEY", "change-me")
UI_PASSWORD = os.getenv("UI_PASSWORD")

HALTER_FIELDS = [
    ("vorname", "Vorname"), ("nachname", "Nachname"), ("strasse", "Strasse"),
    ("hausnummer", "Nr."), ("plz", "PLZ"), ("ort", "Ort"),
    ("versicherung", "Versicherung"), ("geburtsdatum", "Geburtsdatum"),
]
FAHRZEUG_FIELDS = [
    ("kontrollschild", "Kontrollschild"), ("kontrollschildfarbe", "Kontrollschildfarbe"),
    ("fahrzeugart", "Fahrzeugart"), ("marke", "Marke"), ("modell", "Modell"),
    ("vin", "Chassisnummer (VIN)"), ("karosserie", "Karosserie"), ("farbe", "Farbe"),
    ("stammnummer", "Stammnummer"), ("typengenehmigung", "Typengenehmigung"),
    ("hubraum_cm3", "Hubraum (cm³)"), ("leistung_kw", "Leistung (kW)"),
    ("leergewicht_kg", "Leergewicht (kg)"), ("gesamtgewicht_kg", "Gesamtgewicht (kg)"),
    ("erste_inverkehrsetzung", "1. Inverkehrsetzung"), ("emissionscode", "Emissionscode"),
    ("plaetze", "Plätze"),
]


def _gate() -> bool:
    if not gate_enabled(UI_PASSWORD):
        return True
    if st.session_state.get("authed"):
        return True
    pw = st.text_input("Passwort", type="password")
    if st.button("Anmelden"):
        if check_password(pw, UI_PASSWORD):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Falsches Passwort.")
    return False


def _render_section(title, fields, values, section, warned, confidence):
    st.subheader(title)
    cols = st.columns(2)
    for i, (key, label) in enumerate(fields):
        path = f"{section}.{key}"
        badges = ""
        if path in warned:
            badges += " ⚠️"
        conf = confidence.get(path)
        if conf in ("low", "medium"):
            badges += f" ({conf})"
        v = values.get(key)
        cols[i % 2].text_input(label + badges, value="" if v is None else str(v), key=f"f_{path}")


def main():
    st.set_page_config(page_title="Fahrzeugausweis Extraktion", layout="wide")
    st.title("Fahrzeugausweis – Datenextraktion")
    if not _gate():
        return

    uploaded = st.file_uploader(
        "Fahrzeugausweis hochladen", type=["jpg", "jpeg", "png", "heic", "pdf"]
    )
    if not uploaded:
        return

    left, right = st.columns([1, 2])
    with left:
        if uploaded.type and uploaded.type.startswith("image/"):
            st.image(uploaded, caption="Hochgeladen", use_container_width=True)
        else:
            st.info(f"Datei: {uploaded.name}")

    data = uploaded.getvalue()
    with st.spinner("Extrahiere Daten …"):
        t0 = time.perf_counter()
        try:
            result = extract(API_URL, API_KEY, uploaded.name, data, uploaded.type or "application/octet-stream")
        except ApiError as e:
            st.error(f"Extraktion fehlgeschlagen: {e.detail}")
            return
        except Exception as e:  # noqa: BLE001
            st.error(f"Verbindungsfehler: {e}")
            return
        elapsed = time.perf_counter() - t0

    warned = {w["field"] for w in result.get("warnings", [])}
    confidence = result.get("field_confidence", {})

    with right:
        st.success(f"Ausgelesen in {elapsed:.1f}s – bitte prüfen und bei Bedarf korrigieren.")
        _render_section("Halter", HALTER_FIELDS, result.get("halter", {}), "halter", warned, confidence)
        _render_section("Fahrzeug", FAHRZEUG_FIELDS, result.get("fahrzeug", {}), "fahrzeug", warned, confidence)
        hinweise = result.get("interne_hinweise")
        if hinweise:
            st.text_area("Interne Hinweise", value=hinweise)
        with st.expander("Rohdaten (JSON)"):
            st.json(result)


if __name__ == "__main__":
    main()
```

- [ ] **Step 9: Manually verify the UI against a locally-running API**

Run (in two terminals, with `.env` filled):
```bash
# terminal 1 — API
uvicorn app.main:app --port 8080
# terminal 2 — UI
EXTRACTOR_API_URL=http://localhost:8080 EXTRACTOR_API_KEY="$(grep EXTRACTOR_API_KEY .env | cut -d= -f2)" streamlit run ui/app.py
```
Expected: upload `tests/fixtures/Fahrzeugausweis.jpeg` → form fills with plate `SZ 41719`, `marke` VW, `kontrollschildfarbe` weiss; no warnings on a clean read.

- [ ] **Step 10: Commit**

```bash
git add ui/ tests/test_ui_client.py tests/test_ui_auth.py requirements-dev.txt
git commit -m "feat: add Streamlit UI as HTTP client of the extract API"
```

---

### Task 13: UI containerization + deployment

**Files:**
- Create: `ui/Dockerfile`
- Create: `provision_ui.sh`
- Create: `deploy_ui.sh`
- Modify: `README.md` (add UI local-run + deployment sections)

**Interfaces:**
- Produces: a runnable UI image and scripts to provision + deploy `ca-garagenhub-ui`. No new tests (verified via `docker build`).

- [ ] **Step 1: Write `ui/Dockerfile`**

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app

COPY ui/requirements.txt ./ui/requirements.txt
RUN pip install --no-cache-dir -r ui/requirements.txt

COPY ui/ ./ui/

EXPOSE 8501
CMD ["streamlit", "run", "ui/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

- [ ] **Step 2: Write `provision_ui.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: ./provision_ui.sh <image-tag>
# Requires in env: EXTRACTOR_API_URL EXTRACTOR_API_KEY  (optional: UI_PASSWORD)
IMAGE_TAG="${1:?Usage: ./provision_ui.sh <image-tag>}"
RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
ENV_NAME="cae-3c-invoice"
APP="ca-garagenhub-ui"
IMAGE_REPO="garagenhub-ui"

acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
echo "==> Building ${IMAGE_REPO}:${IMAGE_TAG} in ACR (ui/Dockerfile)..."
az acr build --registry "$ACR_NAME" --image "${IMAGE_REPO}:${IMAGE_TAG}" --file ui/Dockerfile .

if az containerapp show --name "$APP" --resource-group "$RG" >/dev/null 2>&1; then
  echo "==> $APP already exists; use ./deploy_ui.sh to update."
  exit 0
fi

echo "==> Creating $APP..."
az containerapp create \
  --name "$APP" --resource-group "$RG" --environment "$ENV_NAME" \
  --image "${acr_server}/${IMAGE_REPO}:${IMAGE_TAG}" \
  --target-port 8501 --ingress external \
  --min-replicas 1 --max-replicas 2 \
  --cpu 0.5 --memory 1.0Gi \
  --secrets api-key="$EXTRACTOR_API_KEY" ui-password="${UI_PASSWORD:-}" \
  --env-vars \
    EXTRACTOR_API_URL="$EXTRACTOR_API_URL" \
    EXTRACTOR_API_KEY=secretref:api-key \
    UI_PASSWORD=secretref:ui-password
echo "==> Done. UI endpoint:"
az containerapp show --name "$APP" --resource-group "$RG" --query properties.configuration.ingress.fqdn -o tsv
```

- [ ] **Step 3: Write `deploy_ui.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail
# Usage: ./deploy_ui.sh <image-tag>   (use a UNIQUE tag)
IMAGE_TAG="${1:?Usage: ./deploy_ui.sh <image-tag>}"
RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
APP="ca-garagenhub-ui"
IMAGE_REPO="garagenhub-ui"

acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
echo "==> Building ${IMAGE_REPO}:${IMAGE_TAG} in ACR (ui/Dockerfile)..."
az acr build --registry "$ACR_NAME" --image "${IMAGE_REPO}:${IMAGE_TAG}" --file ui/Dockerfile .
echo "==> Updating $APP..."
az containerapp update --name "$APP" --resource-group "$RG" \
  --image "${acr_server}/${IMAGE_REPO}:${IMAGE_TAG}"
echo "==> Done."
```

- [ ] **Step 4: Make scripts executable and verify the UI image builds**

Run:
```bash
chmod +x provision_ui.sh deploy_ui.sh
docker build -f ui/Dockerfile -t garagenhub-ui:local .
```
Expected: image builds successfully.

- [ ] **Step 5: Append UI sections to `README.md`**

```markdown
## UI (Streamlit)

Local (API must be running on :8080):
    pip install -r ui/requirements.txt
    EXTRACTOR_API_URL=http://localhost:8080 EXTRACTOR_API_KEY=change-me streamlit run ui/app.py

Set `UI_PASSWORD` to enable a shared-password gate (leave unset for open local dev).

### UI deployment (Azure Container Apps)

Deploys as `ca-garagenhub-ui`, separate from the API, with its own DNS route.

First time (point it at the API's route):
    export EXTRACTOR_API_URL=https://<ca-garagenhub-fqdn> EXTRACTOR_API_KEY=... UI_PASSWORD=...
    ./provision_ui.sh v20260706

Subsequent deploys (unique tag):
    ./deploy_ui.sh v20260707
```

- [ ] **Step 6: Commit**

```bash
git add ui/Dockerfile provision_ui.sh deploy_ui.sh README.md
git commit -m "feat: add UI Dockerfile and provision/deploy scripts for ca-garagenhub-ui"
```

---

## Self-Review

**Spec coverage:**
- Sync single-image endpoint → Task 9. ✓
- Preprocessing (HEIC/PDF/downscale 1600, detail high) → Tasks 6, 7. ✓
- Single vision call, temp 0/seed, tenacity → Task 7. ✓
- Output contract with field-splitting (vorname/nachname, marke/modell, strasse/hausnummer) → Tasks 3 (prompt), 4 (models). ✓
- warnings (deterministic) + field_confidence (model soft) → Tasks 5, 8. ✓
- Numeric coercion (strip `**`) → Tasks 5, 8. ✓
- AXA form field coverage → Tasks 3/4 field lists. ✓
- Error handling (400/401/502, reparse) → Tasks 7, 9. ✓
- Privacy (no persistence, metadata-only logs) → Task 9 logging; no storage anywhere. ✓
- Stateless, always-warm deploy (min-replicas 1) → Task 11. ✓
- Single-tenant now (one API key) → Task 9 auth. ✓
- Testing (unit mocked + opt-in integration + CLI) → Tasks 2–10. ✓
- Data-residency open question → carried into README (Task 11) + spec. ✓
- Streamlit UI as pure HTTP client of `/extract` (no direct import/Azure) → Task 12 (`ui/api_client.py`). ✓
- UI review screen: AXA-shaped Halter/Fahrzeug sections, editable fields, warning ⚠️ + confidence badges, image preview, raw-JSON expander → Task 12 (`ui/app.py`). ✓
- Optional shared-password gate (`UI_PASSWORD`), off when unset → Task 12 (`ui/auth.py`). ✓
- Same repo, separate `ui/Dockerfile` → `ca-garagenhub-ui`, independent DNS route → Task 13. ✓
- UI env (`EXTRACTOR_API_URL`, `EXTRACTOR_API_KEY`, `UI_PASSWORD`) → Tasks 12/13 + README. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. ✓

**Type consistency:** `NormalizedImage(b64, mime, page_count)`, `extract_fields(...) -> (raw, usage)`, `build_response(raw, *, model, processing_ms, image_pages)`, `FIELD_PATHS`/`NUMERIC_FIELDS`, and the model field names are used identically across Tasks 3–10. UI: `extract(api_url, api_key, filename, data, content_type)`, `ApiError(status, detail)`, `gate_enabled`/`check_password` used consistently across Tasks 12–13. Note: Task 12 modifies `requirements-dev.txt` (adds `requests`) created in Task 1. ✓
