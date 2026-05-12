# Multi-Product Platform Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the existing veterinary-only extraction pipeline into a multi-product platform where `vetcostcheck`, `bps`, and `sanierer` can each deploy independently from a shared `core/` library, and migrate `vetcostcheck` to its own pair of new Container Apps.

**Architecture:** Monorepo with `core/` (shared framework — API, jobs, OCR, storage, processors, pipeline) and `products/<name>/` (per-product `ProductConfig`, prompts, schemas). One parameterized Dockerfile produces product-specific images. Each product deploys to its own `ca-api-<product>` + `ca-worker-<product>` pair within the existing `cae-3c-invoice` Container Apps environment.

**Tech Stack:** Python 3.11, FastAPI, RQ, Redis, Azure Container Apps, Azure OpenAI, Azure Document Intelligence, Mistral OCR, Azure Blob Storage.

**Spec:** `docs/superpowers/specs/2026-05-07-multi-product-extraction-design.md`

**Scope of this plan:** Phases 0, 1, and 2 of the spec — repo prep, refactor, and vetcostcheck cutover. Phase 3 (`bps`) and Phase 4 (`sanierer`) are deferred until their domain prompts exist; each will get its own follow-on plan.

---

## Task 1: Rename GitHub repository

**Files:**
- Modify: `README.md`

- [x] **Step 1: Rename the repo on GitHub**

```bash
gh repo rename 3c-extraction-platform
```

GitHub auto-redirects the old URL, so existing clones keep working without action.

- [x] **Step 2: Update local working copy's remote URL**

```bash
git remote set-url origin "$(gh repo view --json sshUrl -q .sshUrl)"
git remote -v
```

Expected: `origin` now points at `git@github.com:<owner>/3c-extraction-platform.git`.

- [x] **Step 3: Update README.md to reflect multi-product scope**

Replace the existing README contents with:

```markdown
# 3C Extraction Platform

Document-extraction platform serving multiple insurance products. Each product (currently: `vetcostcheck`; planned: `bps`, `sanierer`) is implemented as a separate Container App pair on top of a shared `core/` library.

- `core/` — shared framework: API, RQ worker, OCR, storage, LLM processors, pipeline
- `products/<name>/` — per-product `ProductConfig`, prompts, schemas
- `docs/superpowers/specs/` — design specs
- `docs/superpowers/plans/` — implementation plans

See `CLAUDE.md` for development setup and `docs/superpowers/specs/2026-05-07-multi-product-extraction-design.md` for the platform design.
```

- [x] **Step 4: Commit**

```bash
git add README.md
git commit -m "Rename repo, update README for multi-product platform scope"
```

---

## Task 2: Create empty `core/` and `products/vetcostcheck/` skeleton

**Files:**
- Create: `core/__init__.py`
- Create: `products/__init__.py`
- Create: `products/vetcostcheck/__init__.py`

- [x] **Step 1: Create the directories with empty `__init__.py` files**

```bash
mkdir -p core products/vetcostcheck
touch core/__init__.py products/__init__.py products/vetcostcheck/__init__.py
```

- [x] **Step 2: Verify nothing is broken**

```bash
python -c "import core; import products; import products.vetcostcheck; print('imports ok')"
```

Expected: `imports ok`.

- [x] **Step 3: Commit**

```bash
git add core/__init__.py products/__init__.py products/vetcostcheck/__init__.py
git commit -m "Add empty core/ and products/vetcostcheck/ skeleton"
```

---

## Task 3: Build the regression safety net

The Phase 1 refactor must preserve byte-identical extraction behavior. This task creates a small script that runs extraction on fixed PDFs and diffs the JSON against pinned references. It runs once before the refactor (to capture references) and after each refactor step (to confirm parity).

**Files:**
- Create: `scripts/__init__.py`
- Create: `scripts/regression_check.py`
- Create: `tests/regression/references/` (directory for pinned JSON outputs)
- Create: `tests/regression/inputs/` (symlink or directory for test PDFs)

- [x] **Step 1: Choose 3–5 test PDFs**

Pick representative samples from `3C_testdaten_pdf/` covering: a single-invoice document, a multi-invoice PDF, and a document containing a pharmacy receipt or other edge case. Confirm each is currently in production use.

```bash
mkdir -p tests/regression/inputs tests/regression/references
ls 3C_testdaten_pdf/
# Pick ~3 files. Example:
cp 3C_testdaten_pdf/VCC_Viele_Dokumente.pdf tests/regression/inputs/
cp 3C_testdaten_pdf/<file2>.pdf tests/regression/inputs/
cp 3C_testdaten_pdf/<file3>.pdf tests/regression/inputs/
```

- [x] **Step 2: Write the regression check script**

Create `scripts/regression_check.py`:

```python
"""Run extraction on fixed PDFs and diff JSON against pinned references.

Usage:
    python scripts/regression_check.py            # diff against references
    python scripts/regression_check.py --capture  # overwrite references (use before refactor)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

INPUTS = REPO_ROOT / "tests" / "regression" / "inputs"
REFS = REPO_ROOT / "tests" / "regression" / "references"


def run_extraction(pdf_path: Path) -> dict:
    """Run the production pipeline on a single PDF and return the extraction JSON.

    Imports are deferred so this script does not require all production env vars
    until extraction actually runs.
    """
    from storage.storage import LocalStorage
    from storage.file_storage import save_upload
    from jobs.tasks import process_file

    # Save the PDF as if it had been uploaded, then run process_file end-to-end.
    with pdf_path.open("rb") as fh:
        file_id = save_upload(fh, filename=pdf_path.name, storage=LocalStorage(base_dir=REPO_ROOT / "temp"))
    return process_file(file_id)


def diff(pdf_path: Path, capture: bool) -> bool:
    ref_path = REFS / f"{pdf_path.stem}.json"
    actual = run_extraction(pdf_path)

    if capture:
        ref_path.write_text(json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True))
        print(f"[CAPTURED] {pdf_path.name} -> {ref_path}")
        return True

    if not ref_path.exists():
        print(f"[MISSING REF] {pdf_path.name} — run with --capture first", file=sys.stderr)
        return False

    expected = json.loads(ref_path.read_text())
    actual_norm = json.loads(json.dumps(actual, sort_keys=True))
    expected_norm = json.loads(json.dumps(expected, sort_keys=True))
    if actual_norm == expected_norm:
        print(f"[PASS] {pdf_path.name}")
        return True
    print(f"[FAIL] {pdf_path.name}", file=sys.stderr)
    # Print a tiny structural diff for triage
    print(json.dumps({"expected_keys": sorted(expected_norm.keys()) if isinstance(expected_norm, dict) else "...",
                      "actual_keys": sorted(actual_norm.keys()) if isinstance(actual_norm, dict) else "..."},
                     indent=2), file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", action="store_true", help="Overwrite references with current output")
    args = parser.parse_args()

    pdfs = sorted(INPUTS.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {INPUTS}", file=sys.stderr)
        return 2

    all_ok = True
    for pdf in pdfs:
        ok = diff(pdf, capture=args.capture)
        all_ok = all_ok and ok

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

If `save_upload` doesn't exist with that signature, look at how `api/routes/upload.py` saves uploads and call the same helper. The point is to get a `file_id` back and pass it to `process_file()`.

- [x] **Step 3: Capture initial references against current main**

```bash
python scripts/regression_check.py --capture
ls tests/regression/references/
```

Expected: one JSON file per PDF in `tests/regression/inputs/`. **This is the snapshot of pre-refactor behavior.** Do not edit these files during the refactor.

- [x] **Step 4: Confirm the check is wired**

```bash
python scripts/regression_check.py
```

Expected: `[PASS]` for every PDF, exit code 0. (LLM responses are non-deterministic in general, but the project uses `seed=42` + `temperature=0`, so byte-equal outputs are expected on the same model.)

If `[FAIL]` or non-deterministic output: investigate. The seeds may not be wired through every LLM call, or the test PDFs may be too sensitive. Trim the asserted shape (e.g., compare structural keys + numeric totals only) to make it stable before proceeding.

- [x] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/regression_check.py tests/regression/references/ tests/regression/inputs/
git commit -m "Add regression_check.py with pinned references (pre-refactor baseline)"
```

**Deviations from plan as executed:**
- `save_upload` signature in actual code is `(file_bytes, original_filename=None, content_type=None)` (uses env-driven storage), not the plan's `(fh, filename=, storage=)`. Script adapted.
- LLM determinism (seed=42 + temperature=0) does not hold on Azure OpenAI gpt-5.4 deployment. Byte-equal output is unreliable.
- Comparator is structural+numeric only, with smoothers: `.warnings` subtree skipped, `None ↔ str` treated as same shape, length differences on `.clinicians` and `.diagnoses` ignored. See `scripts/regression_check.py` module docstring.
- `VCC_Viele_Dokumente.pdf` (multi-invoice, 4 subdocs, 22-item subdoc) was too LLM-noisy even for structural+numeric. Special-cased to shape-only check (top keys + `number_of_subdocuments` + per-subdoc items count).
- Two consecutive PASS runs confirmed stability before committing.
- `tests/regression/inputs/` and `tests/regression/references/` are NOT committed (customer-shaped data; operator re-captures locally each checkpoint).

---

## Task 4: Thin-slice refactor — move `utils.py` and `config.py` into `core/`

The smallest possible refactor that exercises the full move-and-update-imports loop. If this works end-to-end (regression check passes, image builds, app boots), the layout is validated and the larger moves follow the same pattern.

**Files:**
- Move: `utils.py` → `core/utils.py`
- Move: `config.py` → `core/config.py`
- Modify: any file importing `utils` or `config` (use grep to find)

- [x] **Step 1: Move the files preserving git history**

```bash
git mv utils.py core/utils.py
git mv config.py core/config.py
```

- [x] **Step 2: Find all imports that need updating**

```bash
grep -rn -E "^(from|import) (utils|config)( |\.|$)" --include="*.py" \
  | grep -v -E "(\.venv/|__pycache__/|core/(utils|config)\.py)"
```

Note: imports inside `core/` itself can use relative form (`from .utils import ...`) or absolute (`from core.utils import ...`). Pick one style and apply consistently — recommend absolute (`from core.utils import ...`) for clarity.

- [x] **Step 3: Rewrite the imports**

For every match from step 2, change:
- `from utils import X` → `from core.utils import X`
- `import utils` → `import core.utils as utils` (or rewrite call sites if cleaner)
- `from config import X` → `from core.config import X`
- `import config` → `import core.config as config`

Do this with `sed` for bulk, then review the diff:

```bash
# Bulk substitution:
git grep -l -E "^from utils import" -- '*.py' | xargs -I{} sed -i '' 's/^from utils import/from core.utils import/' {}
git grep -l -E "^from config import" -- '*.py' | xargs -I{} sed -i '' 's/^from config import/from core.config import/' {}
git grep -l -E "^import utils$" -- '*.py' | xargs -I{} sed -i '' 's/^import utils$/import core.utils as utils/' {}
git grep -l -E "^import config$" -- '*.py' | xargs -I{} sed -i '' 's/^import config$/import core.config as config/' {}
```

(macOS `sed` requires `''` after `-i`. On Linux drop the `''`.)

Review:

```bash
git diff --stat
git diff
```

- [x] **Step 4: Verify imports resolve**

```bash
python -c "from core.utils import log_retry; from core.config import REDIS_URL; print('ok')"
```

Expected: `ok`. If `ImportError`, fix the offending file before proceeding.

- [x] **Step 5: Run the regression check**

```bash
docker compose up redis -d
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 python scripts/regression_check.py
```

Expected: `[PASS]` for every PDF.

- [x] **Step 6: Commit**

```bash
git add -u
git commit -m "Move utils.py and config.py into core/ (thin-slice refactor)"
```

**Deviations from plan as executed:**
- No `import utils` / `import config` form existed; only `from X import Y`. The two sed lines for the bare-`import` form were no-ops.
- 9 files had imports rewritten: `invoice.py`, `jobs/tasks.py`, `api/dependencies.py`, `ocr/{ocr_mistral.py,ocr_mistral_v2.py,ocr_azure_docintel.py,ocr_dual.py}`, `processors/{azure_processor.py,gpt_processor.py}`.
- `BASE_DIR` and `FILES_DIR` in `core/config.py` now resolve relative to `core/` instead of repo root (creates `core/files/` on import instead of `files/`). Nothing references either constant elsewhere; dead-code-effect side effect.
- Regression check (`scripts/regression_check.py`) used as the safety gate against pre-refactor references — all 3 PASS.

---

## Task 5: Define `ProductConfig` and the loader

**Files:**
- Create: `core/product.py`
- Create: `tests/core/__init__.py`
- Create: `tests/core/test_product.py`

- [x] **Step 1: Write `core/product.py`**

```python
"""Product configuration — the single contract between core and a product."""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ProductConfig:
    """Configuration injected by a product directory into core.

    A product owns its extraction prompt + schema and may optionally override
    the analysis/splitting prompt + schema. Everything else (OCR, storage,
    queue mechanics, API surface) is shared.
    """
    name: str

    # Extraction stage — REQUIRED
    extract_prompt_builder: Callable[..., str]
    extract_output_schema: dict

    # Analysis/splitting stage — OPTIONAL (falls back to core defaults)
    analyze_prompt_builder: Callable[..., str] | None = None
    analyze_output_schema: dict | None = None

    # Free-form bag for product-specific knobs that don't deserve their own field yet
    extra: dict[str, Any] = field(default_factory=dict)


def load_product_config(name: str | None = None) -> ProductConfig:
    """Load `products.<name>.product:CONFIG`.

    `name` defaults to the `PRODUCT_NAME` env var. Each Container App sets this
    once at deployment time; locally it's set per `docker compose` invocation.
    """
    name = name or os.environ.get("PRODUCT_NAME")
    if not name:
        raise RuntimeError(
            "PRODUCT_NAME is not set. Each product's Container App must set it; "
            "for local dev, e.g. `PRODUCT_NAME=vetcostcheck docker compose up`."
        )
    module = importlib.import_module(f"products.{name}.product")
    config = getattr(module, "CONFIG", None)
    if not isinstance(config, ProductConfig):
        raise RuntimeError(
            f"products.{name}.product must export CONFIG: ProductConfig (got {type(config).__name__})"
        )
    return config
```

- [x] **Step 2: Write a smoke test for the loader**

Create `tests/core/test_product.py`:

```python
import os
import pytest
from core.product import ProductConfig, load_product_config


def test_load_requires_product_name(monkeypatch):
    monkeypatch.delenv("PRODUCT_NAME", raising=False)
    with pytest.raises(RuntimeError, match="PRODUCT_NAME"):
        load_product_config()


def test_load_with_explicit_name_only(monkeypatch, tmp_path):
    monkeypatch.delenv("PRODUCT_NAME", raising=False)
    with pytest.raises(ModuleNotFoundError):
        load_product_config("nonexistent_product")
```

(Add `tests/__init__.py` and `tests/core/__init__.py` if not already present.)

- [x] **Step 3: Run the test**

```bash
pip install pytest 2>/dev/null
python -m pytest tests/core/test_product.py -v
```

Expected: 2 passed.

- [x] **Step 4: Commit**

```bash
git add core/product.py tests/__init__.py tests/core/__init__.py tests/core/test_product.py
git commit -m "Add ProductConfig dataclass and PRODUCT_NAME-based loader"
```

**Deviations from plan as executed:**
- `pytest` installed into the uv-managed `.venv` via `uv pip install pytest` (the plan's `pip install` flag did not apply — the venv is uv-managed). Not added to `requirements*.txt` since the test runner is dev-only and the prod containers do not need it.

---

## Task 6: Create `products/vetcostcheck/` with prompt, schema, and `CONFIG`

Migrates the hardcoded German vet extraction prompt out of `prompt_building/prompt_building.py` and into the vetcostcheck product. Also captures the analyze override that adds `invoice_animals`.

**Files:**
- Create: `products/vetcostcheck/extract_prompt.py`
- Create: `products/vetcostcheck/extract_schema.json`
- Create: `products/vetcostcheck/analyze_overrides.py`
- Create: `products/vetcostcheck/product.py`
- Modify: `prompt_building/prompt_building.py` (mark vet-specific functions as deprecated; will be deleted in Task 9)

- [x] **Step 1: Capture the existing extraction prompt**

Read `prompt_building/prompt_building.py:get_full_prompt(...)`. It builds a single string by combining a static system prompt, optional OCR text, and optional animal information. Move the body into `products/vetcostcheck/extract_prompt.py`:

```python
"""Veterinary invoice extraction prompt (German). Migrated from prompt_building.get_full_prompt."""
from __future__ import annotations


def build_extract_prompt(
    *,
    ocr_text: str = "",
    animal_information: dict | None = None,
    expected_items: int | None = None,
) -> str:
    """Build the extraction prompt for a single vet sub-invoice.

    Signature matches the existing get_full_prompt(...) so the migration is
    behavior-preserving.
    """
    # PASTE the body of get_full_prompt() here, replacing the function name
    # and adjusting parameter names if needed. Do not modify wording.
    ...
```

Open `prompt_building/prompt_building.py` and `get_full_prompt`, copy its full body verbatim into `build_extract_prompt`. The string literal is the contract — do not paraphrase, do not "improve" wording during the move. Byte-identical or the regression check will fail.

- [x] **Step 2: Capture the extraction schema**

The existing extraction is described in `configs/extraction_config.json`. Inspect it:

```bash
cat configs/extraction_config.json
```

If it already contains a clean JSON schema for the vet output, copy it to `products/vetcostcheck/extract_schema.json`. If it's a hybrid prompt-config file, extract just the schema portion. The schema's purpose is documentation + future validation — it does not need to be wired into the LLM call yet.

```bash
# After extracting the schema portion to products/vetcostcheck/extract_schema.json:
python -c "import json; print(json.load(open('products/vetcostcheck/extract_schema.json')).keys())"
```

Expected: schema-shaped keys (e.g. `properties`, `required`, etc., or whatever the existing config uses).

- [x] **Step 3: Capture the analyze override (the `invoice_animals` field)**

Read `prompt_building/prompt_building.py:build_prompt_for_analyze_document`. If the analyze prompt is already config-driven and generic, no override is needed at this stage and you can skip this file. If it bakes in `invoice_animals` or other vet-only schema fields, copy the full body into:

```python
# products/vetcostcheck/analyze_overrides.py
"""Vet-specific analyze prompt + schema overrides."""
from __future__ import annotations


def build_analyze_prompt(*, markdown_text: str = "") -> str:
    """Vet analyze prompt. Adds invoice_animals to the per-subdocument output."""
    # PASTE the body of build_prompt_for_analyze_document() here verbatim.
    ...


ANALYZE_OUTPUT_SCHEMA: dict = {
    # If a schema exists for the analyze output, paste it here. Otherwise: {}
}
```

If the analyze prompt is generic (no vet-specific schema fields), set `analyze_prompt_builder=None` and `analyze_output_schema=None` in Task 6 step 4.

- [x] **Step 4: Write `products/vetcostcheck/product.py`**

```python
"""vetcostcheck ProductConfig — vet invoice extraction."""
from __future__ import annotations

import json
from pathlib import Path

from core.product import ProductConfig
from products.vetcostcheck.extract_prompt import build_extract_prompt

_HERE = Path(__file__).resolve().parent

with (_HERE / "extract_schema.json").open() as fh:
    _EXTRACT_SCHEMA = json.load(fh)

# Load analyze override only if it exists
try:
    from products.vetcostcheck.analyze_overrides import build_analyze_prompt, ANALYZE_OUTPUT_SCHEMA
    _ANALYZE_BUILDER = build_analyze_prompt
    _ANALYZE_SCHEMA = ANALYZE_OUTPUT_SCHEMA or None
except ImportError:
    _ANALYZE_BUILDER = None
    _ANALYZE_SCHEMA = None


CONFIG = ProductConfig(
    name="vetcostcheck",
    extract_prompt_builder=build_extract_prompt,
    extract_output_schema=_EXTRACT_SCHEMA,
    analyze_prompt_builder=_ANALYZE_BUILDER,
    analyze_output_schema=_ANALYZE_SCHEMA,
)
```

- [x] **Step 5: Verify the config loads**

```bash
PRODUCT_NAME=vetcostcheck python -c "
from core.product import load_product_config
c = load_product_config()
print(c.name, type(c.extract_prompt_builder).__name__, list(c.extract_output_schema.keys())[:3])
"
```

Expected: `vetcostcheck function ['<schema-keys>'...]` — no errors.

- [x] **Step 6: Verify behavior is preserved (no functional swap yet)**

The pipeline is still using `prompt_building.get_full_prompt(...)` directly — the swap happens in Task 10. So the regression check should still pass at this stage:

```bash
python scripts/regression_check.py
```

Expected: `[PASS]`.

- [x] **Step 7: Commit**

```bash
git add products/vetcostcheck/
git commit -m "Add vetcostcheck ProductConfig with migrated prompt and schema"
```

**Deviations from plan as executed:**
- `extract_prompt.py` is byte-identical to `prompt_building.get_full_prompt` (verified by direct string comparison across 4 input shapes including empty/with-animals/with-expected-items).
- `extract_schema.json` was authored fresh as a clean Draft 2020-12 JSON Schema reflecting the structure embedded in the prompt's `JSON-Ziel-Schema` block. The legacy `configs/extraction_config.json:extraction_fields` (field-description format) does not match the actual output — it was orphaned by `get_full_prompt` and is retired by this migration.
- `analyze_overrides.py` exports a non-empty `ANALYZE_OUTPUT_SCHEMA` reflecting the analyze output (incl. `invoice_animals` vet-only field), and `build_analyze_prompt` byte-identical to `prompt_building.build_prompt_for_analyze_document`.
- `product.py` imports `analyze_overrides` directly rather than the plan's try/except ImportError fallback — vetcostcheck always has the override, no reason to defer to runtime.
- Regression: 3/3 PASS. Pipeline still uses old `prompt_building` import path; functional swap happens in Task 9.

---

## Task 7: Move `api/`, `jobs/`, `ocr/`, `storage/`, `processors/`, `prompt_building/` into `core/`

A single bulk move that relocates the rest of the Python source. Each move follows the same pattern as Task 4 (git mv + bulk import rewrite + regression check + commit). Done as one task because the moves are mechanically identical and the regression check is the single safety net.

**Files:**
- Move: `api/` → `core/api/`
- Move: `jobs/` → `core/jobs/`
- Move: `ocr/` → `core/ocr/`
- Move: `storage/` → `core/storage/`
- Move: `processors/` → `core/processors/`
- Move: `prompt_building/` → `core/prompt_building/`
- Modify: any file importing from those packages

- [ ] **Step 1: Move all six packages**

```bash
git mv api core/api
git mv jobs core/jobs
git mv ocr core/ocr
git mv storage core/storage
git mv processors core/processors
git mv prompt_building core/prompt_building
```

- [ ] **Step 2: Find all imports that need updating**

```bash
grep -rnE "^(from|import) (api|jobs|ocr|storage|processors|prompt_building)( |\.|$)" --include="*.py" \
  | grep -v -E "(\.venv/|__pycache__/|core/)"
```

This lists every file outside `core/` that imports a now-relocated package. Imports *inside* `core/` are also affected — handle them in step 3.

- [ ] **Step 3: Rewrite imports in bulk**

```bash
for pkg in api jobs ocr storage processors prompt_building; do
  git grep -l -E "^from ${pkg}(\.| import)" -- '*.py' \
    | xargs -I{} sed -i '' "s|^from ${pkg}\.|from core.${pkg}.|; s|^from ${pkg} import|from core.${pkg} import|" {}
  git grep -l -E "^import ${pkg}( |$)" -- '*.py' \
    | xargs -I{} sed -i '' "s|^import ${pkg}\$|import core.${pkg} as ${pkg}|; s|^import ${pkg} |import core.${pkg} as ${pkg} |" {}
done
```

- [ ] **Step 4: Review the diff carefully**

```bash
git diff --stat
git diff -- '*.py' | head -200
```

Sanity check: every changed import line should now start with `from core.<pkg>` or `import core.<pkg>`. No orphaned `from api`, `from jobs`, etc.

- [ ] **Step 5: Update Dockerfile CMD if needed**

The current CMD is `uvicorn api.main:app`. After the move it must be `uvicorn core.api.main:app`. Edit `Dockerfile`:

```dockerfile
CMD ["bash", "-lc", "uvicorn core.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
```

(This is a pre-emptive change — Task 12 rewrites the Dockerfile more thoroughly. Do this one-line change now so the regression check container can boot.)

- [ ] **Step 6: Update docker-compose.yml worker command**

The worker's command is currently `python jobs/worker.py`. Update to `python -m core.jobs.worker`:

```yaml
# in docker-compose.yml, worker service:
command: python -m core.jobs.worker
```

The `worker.py` script's `if __name__ == "__main__":` block is still at module level, so `python -m core.jobs.worker` runs it.

- [ ] **Step 7: Smoke-import to catch any missed renames**

```bash
python -c "
from core.api.main import app
from core.jobs.tasks import process_file
from core.ocr.ocr_dual import DualOCRProcessor
from core.storage.storage import LocalStorage
from core.processors.azure_processor import AzureInvoiceProcessor
print('all imports ok')
"
```

Expected: `all imports ok`. If `ImportError`, find and fix the offending file. Common missed cases: imports that span lines, conditional imports, imports inside functions. Use `grep` to find any remaining references like `from api.` or `import jobs.`.

- [ ] **Step 8: Run the regression check**

```bash
docker compose up redis -d
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 python scripts/regression_check.py
```

Expected: `[PASS]` for every PDF.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "Move api/, jobs/, ocr/, storage/, processors/, prompt_building/ into core/"
```

---

## Task 8: Move `invoice.py` into `core/pipeline.py` and rename the class

The `Invoice` class is the central pipeline orchestrator. Renaming both file and class signals that it's a generic per-product pipeline, not vet-specific.

**Files:**
- Move: `invoice.py` → `core/pipeline.py`
- Rename: `class Invoice` → `class Pipeline`
- Modify: every file importing `Invoice` (use grep)

- [ ] **Step 1: Move the file**

```bash
git mv invoice.py core/pipeline.py
```

- [ ] **Step 2: Rename the class inside `core/pipeline.py`**

Open `core/pipeline.py` and rename:
- `class Invoice:` → `class Pipeline:`
- All internal `self` references stay unchanged — only the class name changes.
- Module-level helpers (e.g. `_call_analyze_llm`) stay.

- [ ] **Step 3: Update the import path (mechanical)**

```bash
git grep -l -E "^from invoice import" -- '*.py' \
  | xargs -I{} sed -i '' 's|^from invoice import|from core.pipeline import|' {}
```

- [ ] **Step 4: Find every remaining `Invoice` reference and review by hand**

```bash
git grep -n "Invoice" -- '*.py'
```

Expected matches fall into three categories:

1. **Class references that must be renamed** — `class Invoice:`, constructor calls `Invoice(...)`, type hints `: Invoice`, `isinstance(x, Invoice)`. Edit each by hand to use `Pipeline` instead.
2. **String literals** that mention "Invoice" (e.g. FastAPI title `"Invoice Extraction API"`, log keys, comments). Leave these alone — they're user-visible/cosmetic and unrelated to the class rename.
3. **The class definition itself** in `core/pipeline.py` — change `class Invoice:` to `class Pipeline:`.

After editing, run the same grep again and confirm the remaining matches are all category 2 (string literals/comments).

```bash
git grep -n "Invoice" -- '*.py'
git diff -- '*.py' | head -100
```

- [ ] **Step 5: Smoke-import**

```bash
python -c "from core.pipeline import Pipeline; print(Pipeline)"
```

Expected: `<class 'core.pipeline.Pipeline'>`.

- [ ] **Step 6: Run the regression check**

```bash
python scripts/regression_check.py
```

Expected: `[PASS]`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "Rename invoice.py to core/pipeline.py; Invoice -> Pipeline"
```

---

## Task 9: Wire `ProductConfig` into `Pipeline` and `process_file`

Now the pipeline actually consults the loaded `ProductConfig` for prompts instead of importing them directly from `core/prompt_building/`. This is the *behavior* migration — until now everything has been file-rearranging.

**Files:**
- Modify: `core/pipeline.py`
- Modify: `core/jobs/tasks.py`
- Delete: vet-specific functions in `core/prompt_building/prompt_building.py` (now in `products/vetcostcheck/`)

- [ ] **Step 1: Modify `core/pipeline.py` to accept a `ProductConfig`**

Update the `Pipeline.__init__` signature to accept `product_config: ProductConfig`:

```python
# Top of core/pipeline.py — add import:
from core.product import ProductConfig

# In Pipeline.__init__, add a parameter:
class Pipeline:
    def __init__(
        self,
        file_key,
        ocr_engine,
        storage,
        output_prefix,
        product_config: ProductConfig,   # NEW
    ):
        ...
        self.product_config = product_config
        ...
```

In `Pipeline.analyze_document(...)`: replace direct calls to `build_prompt_for_analyze_document(...)` with:

```python
if self.product_config.analyze_prompt_builder is not None:
    prompt = self.product_config.analyze_prompt_builder(markdown_text=self.markdown)
else:
    # Fall back to core's generic analyze prompt
    from core.prompt_building.prompt_building import build_prompt_for_analyze_document
    prompt = build_prompt_for_analyze_document(markdown_text=self.markdown)
```

In `Pipeline.extract_data_from_subdocuments(...)`: replace direct calls to `get_full_prompt(...)` with:

```python
prompt = self.product_config.extract_prompt_builder(
    ocr_text=subdoc_ocr_text,
    animal_information=subdoc.get("animal_information", {}),  # vet-only; safe to pass through
    expected_items=subdoc.get("expected_items"),
)
```

(Adapt the kwargs to the actual `build_extract_prompt` signature you wrote in Task 6. The vet builder accepts `animal_information` and `expected_items`; non-vet products' builders may simply ignore them.)

- [ ] **Step 2: Modify `core/jobs/tasks.py:process_file` to load and pass the `ProductConfig`**

Add at the top of `process_file`:

```python
from core.product import load_product_config
# ...
def process_file(file_id: str):
    job_start = time.monotonic()
    invoice = None
    log.info("job_started", file_id=file_id)
    try:
        product_config = load_product_config()  # reads PRODUCT_NAME env var
        log.info("product_loaded", product=product_config.name)
        # ... existing code ...
        invoice = Pipeline(
            file_key=file_key,
            ocr_engine=dual_ocr_engine,
            storage=storage,
            output_prefix=output_prefix,
            product_config=product_config,  # NEW
        )
```

Rename the variable `invoice` → `pipeline` for clarity (optional cosmetic improvement, defer if it bloats the diff).

- [ ] **Step 3: Delete the vet-specific function from `core/prompt_building/prompt_building.py`**

The function `get_full_prompt` is now duplicated in `products/vetcostcheck/extract_prompt.py:build_extract_prompt`. Remove `get_full_prompt` and any other vet-only helpers from `core/prompt_building/prompt_building.py`. Keep the generic `build_prompt_for_analyze_document` (which `core` falls back to when no analyze override is set).

- [ ] **Step 4: Update the regression check to set `PRODUCT_NAME`**

The regression check runs `process_file` directly, which now requires `PRODUCT_NAME`. Update the script's docstring and add an `os.environ.setdefault("PRODUCT_NAME", "vetcostcheck")` at the top of `scripts/regression_check.py`:

```python
import os
os.environ.setdefault("PRODUCT_NAME", "vetcostcheck")
```

- [ ] **Step 5: Run the regression check**

```bash
docker compose up redis -d
STORAGE_BACKEND=local REDIS_URL=redis://localhost:6379/0 python scripts/regression_check.py
```

Expected: `[PASS]` for every PDF. **This is the most important checkpoint in the whole plan** — it confirms that `Pipeline + load_product_config('vetcostcheck')` produces the same JSON as the pre-refactor pipeline.

If `[FAIL]`: diff the new JSON against the reference. Most likely cause: `build_extract_prompt` doesn't reproduce the exact wording of `get_full_prompt`. Fix the prompt text to match exactly.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "Wire ProductConfig into Pipeline; vetcostcheck behaviorally preserved"
```

---

## Task 10: Update `Dockerfile` to be product-parameterized

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Rewrite the Dockerfile**

Replace the current `Dockerfile` with:

```dockerfile
FROM python:3.11-slim

ARG PRODUCT
ENV PRODUCT_NAME=${PRODUCT}

# Tesseract OCR for page orientation detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-deu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

# Shared core
COPY core/ ./core/
COPY products/__init__.py ./products/__init__.py

# Product-specific code only — image contains exactly one product
COPY products/${PRODUCT}/ ./products/${PRODUCT}/

ENV PYTHONPATH="/app:${PYTHONPATH}"

# ACA / Render sets $PORT, default locally is 8000
CMD ["bash", "-lc", "uvicorn core.api.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level info"]
```

Key changes:
- `ARG PRODUCT` accepts a build arg. Build with `docker build --build-arg PRODUCT=vetcostcheck .`
- `ENV PRODUCT_NAME=${PRODUCT}` makes it available at runtime (for `load_product_config()`).
- Only the named product directory is copied — the image is product-specific.
- CMD points at `core.api.main:app` (post-refactor path).

- [ ] **Step 2: Build locally and verify**

```bash
docker build --build-arg PRODUCT=vetcostcheck -t local/3cix-vetcostcheck:test .
docker run --rm -e PRODUCT_NAME=vetcostcheck local/3cix-vetcostcheck:test \
  python -c "from core.product import load_product_config; print(load_product_config().name)"
```

Expected: `vetcostcheck`.

Verify that *only* vetcostcheck is in the image:

```bash
docker run --rm local/3cix-vetcostcheck:test ls products/
```

Expected: `vetcostcheck` only (no `bps`, `sanierer`, or other products).

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "Parameterize Dockerfile with PRODUCT build-arg; image holds only one product"
```

---

## Task 11: Update `deploy.sh` to take `<product> [tag]`

**Files:**
- Modify: `deploy.sh`

- [ ] **Step 1: Rewrite `deploy.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   ./deploy.sh <product> [tag]
#   ./deploy.sh all [tag]
#
# Example:
#   ./deploy.sh vetcostcheck v20260507
#   ./deploy.sh all v20260507

PRODUCT="${1:?Usage: ./deploy.sh <product|all> [tag]}"
IMAGE_TAG="${2:-latest}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
PRODUCTS=("vetcostcheck" "bps" "sanierer")

deploy_one() {
  local product="$1"
  local tag="$2"

  if [[ ! -d "products/${product}" ]]; then
    echo "ERROR: products/${product}/ does not exist" >&2
    return 1
  fi

  local acr_server image_repo image
  acr_server=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
  image_repo="3cix-${product}"
  image="${acr_server}/${image_repo}:${tag}"

  echo "==> [${product}] Building image ${image_repo}:${tag} in ACR..."
  az acr build \
    --registry "$ACR_NAME" \
    --image "${image_repo}:${tag}" \
    --build-arg "PRODUCT=${product}" \
    .

  for kind in api worker; do
    local app="ca-${kind}-${product}"
    if ! az containerapp show --name "$app" --resource-group "$RG" >/dev/null 2>&1; then
      echo "==> [${product}] SKIP: ${app} does not exist yet (run scripts/provision_product.sh ${product} first)"
      continue
    fi
    echo "==> [${product}] Updating ${app}..."
    az containerapp update --name "$app" --resource-group "$RG" --image "$image"
  done
}

if [[ "$PRODUCT" == "all" ]]; then
  for p in "${PRODUCTS[@]}"; do
    if [[ -d "products/${p}" ]]; then
      deploy_one "$p" "$IMAGE_TAG"
    fi
  done
else
  deploy_one "$PRODUCT" "$IMAGE_TAG"
fi

echo ""
echo "==> Done."
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x deploy.sh
```

- [ ] **Step 3: Smoke test (dry run — no Container App update yet)**

The script bails on missing Container Apps with a friendly SKIP message, so it's safe to run. The first real deploy happens in Task 13.

```bash
./deploy.sh vetcostcheck dryrun-test
```

Expected: image builds in ACR, then SKIP messages for `ca-api-vetcostcheck` and `ca-worker-vetcostcheck` because those don't exist yet (Task 13 creates them). The image lands in ACR — confirm:

```bash
az acr repository show-tags --name cr3cinvoice --repository 3cix-vetcostcheck -o tsv
```

Expected: `dryrun-test` is in the list.

- [ ] **Step 4: Commit**

```bash
git add deploy.sh
git commit -m "Rewrite deploy.sh: ./deploy.sh <product|all> [tag]"
```

---

## Task 12: Update `docker-compose.yml` for product-aware local dev

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Rewrite `docker-compose.yml`**

```yaml
services:
  api:
    build:
      context: .
      args:
        PRODUCT: ${PRODUCT:-vetcostcheck}
    container_name: ${PRODUCT:-vetcostcheck}_api
    ports:
      - "8000:8000"
    depends_on:
      - redis
    volumes:
      - .:/app
      - ./temp:/app/temp
    env_file:
      - .env
    environment:
      PRODUCT_NAME: ${PRODUCT:-vetcostcheck}
      STORAGE_BACKEND: "local"
      LOCAL_STORAGE_BASE_DIR: "/app/temp"
      REDIS_URL: "redis://redis:6379/0"
    command: uvicorn core.api.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug

  worker:
    build:
      context: .
      args:
        PRODUCT: ${PRODUCT:-vetcostcheck}
    container_name: ${PRODUCT:-vetcostcheck}_worker
    depends_on:
      - redis
    volumes:
      - .:/app
      - ./temp:/app/temp
    env_file:
      - .env
    environment:
      PRODUCT_NAME: ${PRODUCT:-vetcostcheck}
      STORAGE_BACKEND: "local"
      LOCAL_STORAGE_BASE_DIR: "/app/temp"
      REDIS_URL: "redis://redis:6379/0"
    command: python -m core.jobs.worker

  redis:
    image: redis:7
    container_name: invoice_redis
    ports:
      - "6379:6379"
```

Key changes:
- `args: PRODUCT: ${PRODUCT:-vetcostcheck}` passes the build-arg to Docker.
- `PRODUCT_NAME` env var is set inside the container.
- `command:` points at the post-refactor module paths.
- Container names are prefixed by product so two products can run side-by-side locally if needed.

- [ ] **Step 2: Smoke test default (vetcostcheck)**

```bash
docker compose up --build -d
sleep 5
curl -s http://localhost:8000/healthz
docker compose logs api --tail=20
docker compose down
```

Expected: `/healthz` returns `{"status":"ok"}`. Logs show no import errors.

- [ ] **Step 3: Smoke test with explicit override**

```bash
PRODUCT=vetcostcheck docker compose up --build -d
sleep 5
docker compose exec api python -c "from core.product import load_product_config; print(load_product_config().name)"
docker compose down
```

Expected: `vetcostcheck`.

- [ ] **Step 4: Run the full API integration test**

```bash
docker compose up --build -d
sleep 10
python test_api.py
docker compose down
```

Expected: `test_api.py` completes successfully (uploads a PDF, polls the job, sees `finished` with extraction JSON).

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "Make docker-compose product-aware via PRODUCT env var (default: vetcostcheck)"
```

---

## Task 13: Add per-product smoke test scaffold

**Files:**
- Create: `tests/products/__init__.py`
- Create: `tests/products/vetcostcheck/__init__.py`
- Create: `tests/products/vetcostcheck/test_smoke.py`

- [ ] **Step 1: Write the smoke test**

Create `tests/products/vetcostcheck/test_smoke.py`:

```python
"""Smoke test for the vetcostcheck product.

Verifies that ProductConfig loads, the prompt builder is callable, and the
schema is parseable. Does NOT run an end-to-end extraction (that's what the
regression check is for during the migration).
"""
import os
import pytest
from core.product import load_product_config


def test_vetcostcheck_config_loads(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    assert config.name == "vetcostcheck"
    assert callable(config.extract_prompt_builder)
    assert isinstance(config.extract_output_schema, dict)
    assert config.extract_output_schema  # non-empty


def test_vetcostcheck_extract_prompt_builds_string(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "vetcostcheck")
    config = load_product_config()
    prompt = config.extract_prompt_builder(ocr_text="dummy ocr", animal_information={})
    assert isinstance(prompt, str)
    assert len(prompt) > 100  # the vet prompt is long; sanity check
```

- [ ] **Step 2: Run the test**

```bash
python -m pytest tests/products/vetcostcheck/test_smoke.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/products/__init__.py tests/products/vetcostcheck/
git commit -m "Add vetcostcheck smoke test"
```

---

## Task 14: Write `scripts/provision_product.sh`

This script provisions one product's pair of Container Apps with KEDA scaler, ingress, env vars, and secrets. Run once per new product (Phase 2 vetcostcheck cutover, then later for bps and sanierer).

**Files:**
- Create: `scripts/provision_product.sh`

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Usage: scripts/provision_product.sh <product> <image-tag>
#
# Provisions ca-api-<product> and ca-worker-<product> in the existing
# cae-3c-invoice environment. Idempotent: if the apps already exist, exits.

PRODUCT="${1:?Usage: scripts/provision_product.sh <product> <image-tag>}"
IMAGE_TAG="${2:?Usage: scripts/provision_product.sh <product> <image-tag>}"

RG="rg-3c-invoice"
ACR_NAME="cr3cinvoice"
ENV_NAME="cae-3c-invoice"
LOCATION="germanywestcentral"

API_APP="ca-api-${PRODUCT}"
WORKER_APP="ca-worker-${PRODUCT}"
QUEUE_NAME="jobs-${PRODUCT}"
IMAGE_REPO="3cix-${PRODUCT}"

# Idempotency: skip if both apps exist
if az containerapp show --name "$API_APP" --resource-group "$RG" >/dev/null 2>&1 \
   && az containerapp show --name "$WORKER_APP" --resource-group "$RG" >/dev/null 2>&1; then
  echo "==> Both ${API_APP} and ${WORKER_APP} already exist. Nothing to do."
  exit 0
fi

# Resolve ACR
ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
IMAGE="${ACR_SERVER}/${IMAGE_REPO}:${IMAGE_TAG}"

# --- Required env values (read from local environment / .env) ---
: "${REDIS_URL:?Set REDIS_URL in your shell or .env before running}"
: "${AZURE_STORAGE_ACCOUNT_NAME:?}"
: "${AZURE_STORAGE_ACCOUNT_KEY:?}"
: "${AZURE_ENDPOINT:?}"
: "${AZURE_OPENAI_KEY:?}"
: "${AZURE_OPENAI_API_VERSION:?}"
: "${MISTRAL_API_KEY:?}"
: "${AZURE_DOCINTEL_ENDPOINT:?}"
: "${AZURE_DOCINTEL_KEY:?}"

# Per-product values (caller may override)
INVOICE_API_KEY="${INVOICE_API_KEY:-$(openssl rand -hex 32)}"
SENTRY_DSN="${SENTRY_DSN:-}"

# --- Provision API ---
echo "==> Creating ${API_APP}..."
az containerapp create \
  --name "$API_APP" \
  --resource-group "$RG" \
  --environment "$ENV_NAME" \
  --image "$IMAGE" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --target-port 8000 \
  --ingress external \
  --transport http \
  --cpu 0.5 --memory 1.0Gi \
  --min-replicas 1 --max-replicas 3 \
  --secrets \
    redis-url="$REDIS_URL" \
    storage-account-key="$AZURE_STORAGE_ACCOUNT_KEY" \
    azure-openai-key="$AZURE_OPENAI_KEY" \
    mistral-api-key="$MISTRAL_API_KEY" \
    azure-docintel-key="$AZURE_DOCINTEL_KEY" \
    invoice-api-key="$INVOICE_API_KEY" \
    sentry-dsn="$SENTRY_DSN" \
  --env-vars \
    PRODUCT_NAME="$PRODUCT" \
    RQ_QUEUE_NAME="$QUEUE_NAME" \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_NAME="$AZURE_STORAGE_ACCOUNT_NAME" \
    AZURE_STORAGE_ACCOUNT_KEY=secretref:storage-account-key \
    AZURE_OUTPUT_PREFIX="az://3cixstorage/${PRODUCT}" \
    AZURE_ENDPOINT="$AZURE_ENDPOINT" \
    AZURE_OPENAI_KEY=secretref:azure-openai-key \
    AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
    MISTRAL_API_KEY=secretref:mistral-api-key \
    AZURE_DOCINTEL_ENDPOINT="$AZURE_DOCINTEL_ENDPOINT" \
    AZURE_DOCINTEL_KEY=secretref:azure-docintel-key \
    REDIS_URL=secretref:redis-url \
    INVOICE_API_KEY=secretref:invoice-api-key \
    SENTRY_DSN=secretref:sentry-dsn

# --- Provision Worker ---
echo "==> Creating ${WORKER_APP}..."
az containerapp create \
  --name "$WORKER_APP" \
  --resource-group "$RG" \
  --environment "$ENV_NAME" \
  --image "$IMAGE" \
  --registry-server "$ACR_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASS" \
  --command "python" --args "-m" "core.jobs.worker" \
  --cpu 2.0 --memory 4.0Gi \
  --min-replicas 0 --max-replicas 5 \
  --secrets \
    redis-url="$REDIS_URL" \
    storage-account-key="$AZURE_STORAGE_ACCOUNT_KEY" \
    azure-openai-key="$AZURE_OPENAI_KEY" \
    mistral-api-key="$MISTRAL_API_KEY" \
    azure-docintel-key="$AZURE_DOCINTEL_KEY" \
    sentry-dsn="$SENTRY_DSN" \
  --env-vars \
    PRODUCT_NAME="$PRODUCT" \
    RQ_QUEUE_NAME="$QUEUE_NAME" \
    STORAGE_BACKEND=azure \
    AZURE_STORAGE_ACCOUNT_NAME="$AZURE_STORAGE_ACCOUNT_NAME" \
    AZURE_STORAGE_ACCOUNT_KEY=secretref:storage-account-key \
    AZURE_OUTPUT_PREFIX="az://3cixstorage/${PRODUCT}" \
    AZURE_ENDPOINT="$AZURE_ENDPOINT" \
    AZURE_OPENAI_KEY=secretref:azure-openai-key \
    AZURE_OPENAI_API_VERSION="$AZURE_OPENAI_API_VERSION" \
    MISTRAL_API_KEY=secretref:mistral-api-key \
    AZURE_DOCINTEL_ENDPOINT="$AZURE_DOCINTEL_ENDPOINT" \
    AZURE_DOCINTEL_KEY=secretref:azure-docintel-key \
    REDIS_URL=secretref:redis-url \
    SENTRY_DSN=secretref:sentry-dsn

# --- KEDA scaler on Redis queue length ---
echo "==> Adding KEDA Redis scaler to ${WORKER_APP}..."
az containerapp update \
  --name "$WORKER_APP" \
  --resource-group "$RG" \
  --scale-rule-name "${QUEUE_NAME}-len" \
  --scale-rule-type "redis" \
  --scale-rule-metadata \
    "address=${REDIS_URL#rediss://*@}" \
    "listName=rq:queue:${QUEUE_NAME}" \
    "listLength=1" \
    "enableTLS=true" \
  --scale-rule-auth "password=redis-url"

# Cooldown
az containerapp update \
  --name "$WORKER_APP" \
  --resource-group "$RG" \
  --revision-suffix "scale-$(date +%s)" \
  --scale-rule-cooldown 1200

API_FQDN=$(az containerapp show --name "$API_APP" --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "==> Provisioned ${PRODUCT}:"
echo "    API:    https://${API_FQDN}"
echo "    Worker: ${WORKER_APP}"
echo "    Queue:  ${QUEUE_NAME}"
echo ""
echo "Next: map 3c${PRODUCT}.flex-capital-scale.com to ${API_APP} (custom domain + managed cert)"
```

Note: the KEDA scaler `--scale-rule-metadata` syntax matches Azure CLI's expected list-of-key-value format; if your CLI version differs (it's been changing), consult `az containerapp update --help` and the existing `ca-invoice-worker`'s scaler config for the exact incantation. Run `az containerapp show --name ca-invoice-worker --resource-group rg-3c-invoice --query properties.template.scale -o json` to crib the working configuration.

- [ ] **Step 2: Make it executable**

```bash
chmod +x scripts/provision_product.sh
```

- [ ] **Step 3: Dry-read to verify shell syntax**

```bash
bash -n scripts/provision_product.sh
```

Expected: no output (script is syntactically valid). Real execution waits until Task 15.

- [ ] **Step 4: Commit**

```bash
git add scripts/provision_product.sh
git commit -m "Add scripts/provision_product.sh for per-product Container Apps provisioning"
```

---

## Task 15: Phase 2 — provision new vetcostcheck Container Apps

The existing `ca-invoice-api` and `ca-invoice-worker` keep running. The new `ca-api-vetcostcheck` and `ca-worker-vetcostcheck` are stood up alongside them, reading from the *new* `jobs-vetcostcheck` queue.

**Files:** none (deployment-only step)

- [ ] **Step 1: Tag and push the first product image**

```bash
TAG="v$(date +%Y%m%d)a"
./deploy.sh vetcostcheck "$TAG"
```

Expected: image `cr3cinvoice.azurecr.io/3cix-vetcostcheck:vYYYYMMDDa` lands in ACR. The Container App update steps SKIP because the apps don't exist yet — that's fine.

- [ ] **Step 2: Provision the new apps**

Source the production secrets into your shell (don't echo them). The simplest way is to run from your existing `.env`:

```bash
set -a; source .env; set +a
TAG="v$(date +%Y%m%d)a"
scripts/provision_product.sh vetcostcheck "$TAG"
```

Expected: both `ca-api-vetcostcheck` and `ca-worker-vetcostcheck` are created. Capture the API FQDN from the script's output.

- [ ] **Step 3: Verify the new API**

```bash
API_FQDN=$(az containerapp show --name ca-api-vetcostcheck --resource-group rg-3c-invoice \
  --query "properties.configuration.ingress.fqdn" -o tsv)
curl -s "https://${API_FQDN}/healthz"
curl -s "https://${API_FQDN}/ready"
```

Expected: `{"status":"ok"}` for `/healthz`. `/ready` should report `redis: ok` and `storage: ok`.

- [ ] **Step 4: End-to-end test against the new app**

Use the existing `test_api.py` against the new endpoint. Either edit the script's URL temporarily or run:

```bash
INVOICE_API_BASE_URL="https://${API_FQDN}" \
INVOICE_API_KEY="<the new key from provisioning>" \
python test_api.py
```

(If `test_api.py` doesn't read those env vars, edit it locally to point at the new URL/key — don't commit that change. Or pass them directly via whatever mechanism it accepts.)

Expected: a job is enqueued onto `jobs-vetcostcheck`, the new worker picks it up, and the extraction completes with the same JSON shape as today.

- [ ] **Step 5: No commit needed** (deployment-only). If `test_api.py` was edited locally, revert the change.

---

## Task 16: Phase 2 — switch the custom domain to the new API and drain the old queue

This is the cutover. After this, production vetcostcheck traffic flows through `ca-api-vetcostcheck`, and `ca-invoice-api` / `ca-invoice-worker` enter their drain window.

**Files:** none (deployment-only step)

- [ ] **Step 1: Move the custom domain mapping**

The domain `3cvetcostcheck.flex-capital-scale.com` is currently mapped to `ca-invoice-api`. Move it to `ca-api-vetcostcheck`:

```bash
# Get the current binding details
az containerapp hostname list --name ca-invoice-api --resource-group rg-3c-invoice -o table

# Add the binding to the new app (Azure-managed cert)
az containerapp hostname add \
  --hostname 3cvetcostcheck.flex-capital-scale.com \
  --name ca-api-vetcostcheck \
  --resource-group rg-3c-invoice

# Bind the cert (managed)
az containerapp hostname bind \
  --hostname 3cvetcostcheck.flex-capital-scale.com \
  --name ca-api-vetcostcheck \
  --resource-group rg-3c-invoice \
  --environment cae-3c-invoice \
  --validation-method CNAME

# Once the cert is provisioned (a few minutes), remove it from the old app
az containerapp hostname delete \
  --hostname 3cvetcostcheck.flex-capital-scale.com \
  --name ca-invoice-api \
  --resource-group rg-3c-invoice \
  --yes
```

Verify:

```bash
curl -s https://3cvetcostcheck.flex-capital-scale.com/healthz
```

Expected: `{"status":"ok"}` served from the new app.

- [ ] **Step 2: Confirm new jobs flow into the new queue**

In a separate terminal, watch Sentry and the Container App logs:

```bash
az containerapp logs show --name ca-api-vetcostcheck --resource-group rg-3c-invoice --follow
```

Send an upload+process via the production domain. The log should show `product_loaded product=vetcostcheck` and the job should land on `jobs-vetcostcheck`.

- [ ] **Step 3: Drain the old queue**

The old `ca-invoice-worker` keeps consuming from `invoice-jobs`. Wait for it to drain:

```bash
# Watch the old queue length
docker run --rm -it redis:7 redis-cli -u "$REDIS_URL" LLEN rq:queue:invoice-jobs
```

When it's been `0` for a continuous 30 minutes (and you've sent no manual jobs to it), the queue is drained.

- [ ] **Step 4: Soak for 24–48h**

Watch:
- Sentry (the existing `python-fastapi` project) for new errors
- The new `3cix-vetcostcheck` Sentry project (once you've created it and set its DSN on `ca-api-vetcostcheck` and `ca-worker-vetcostcheck`)
- `az containerapp revision list` for either old or new app entering an unhealthy state

If anything regresses, the rollback is: re-bind the domain to `ca-invoice-api` (reverse of step 1). The old infra is untouched.

- [ ] **Step 5: No commit** (operational-only).

---

## Task 17: Phase 2 — decommission the old Container Apps

After the soak period passes cleanly, remove the old infra.

**Files:** none

- [ ] **Step 1: Delete the old API and worker**

```bash
az containerapp delete --name ca-invoice-api --resource-group rg-3c-invoice --yes
az containerapp delete --name ca-invoice-worker --resource-group rg-3c-invoice --yes
```

- [ ] **Step 2: Confirm clean state**

```bash
az containerapp list --resource-group rg-3c-invoice -o table
```

Expected output includes `ca-api-vetcostcheck` and `ca-worker-vetcostcheck`, no longer includes `ca-invoice-api` or `ca-invoice-worker`.

- [ ] **Step 3: Update CLAUDE.md to reflect the new app names and commands**

Edit `CLAUDE.md` deployment section: update Container App names, mention `PRODUCT_NAME` and the per-product queue, point at `scripts/provision_product.sh` for new products, and note `deploy.sh <product> [tag]`'s new signature.

```bash
# Open and edit CLAUDE.md by hand — the changes are content, not mechanical
$EDITOR CLAUDE.md
```

- [ ] **Step 4: Update TODO.md**

Mark the migration items done; add a "Future" entry pointing at Phase 3 / Phase 4 plans (BPS, Sanierer onboarding) which haven't been written yet.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md TODO.md
git commit -m "Phase 2 complete: vetcostcheck running on ca-api-vetcostcheck/ca-worker-vetcostcheck"
```

---

## Self-Review Checklist (run after completing tasks)

Before declaring this plan done, verify against the spec:

- [ ] `core/` exists and contains `api/`, `jobs/`, `ocr/`, `storage/`, `processors/`, `prompt_building/`, `pipeline.py`, `product.py`, `utils.py`, `config.py`. No vet-specific code remains.
- [ ] `products/vetcostcheck/` exists with `product.py`, `extract_prompt.py`, `extract_schema.json`, optionally `analyze_overrides.py`.
- [ ] `Dockerfile` accepts `PRODUCT` build-arg; image contains exactly one product directory.
- [ ] `deploy.sh <product|all> [tag]` works.
- [ ] `scripts/provision_product.sh <product> <tag>` works (tested by Task 15).
- [ ] `docker-compose.yml` is product-aware via `PRODUCT` env var.
- [ ] Regression check passes for vetcostcheck.
- [ ] `ca-api-vetcostcheck` is serving `3cvetcostcheck.flex-capital-scale.com`.
- [ ] `ca-invoice-api` and `ca-invoice-worker` are deleted.
- [ ] `tests/products/vetcostcheck/test_smoke.py` passes.
- [ ] `CLAUDE.md` and `TODO.md` updated.

## Out of Scope (separate plans)

- **BPS onboarding** (`products/bps/`, prompt iteration, provisioning, custom domain) — separate plan once domain prompts exist.
- **Sanierer onboarding** — same.
- **Migrate `provision_product.sh` to Bicep** — deferred follow-up per spec's Open Questions.
- **Redis resilience improvements** — see `redis_resilience_plan.md`; independent track.
