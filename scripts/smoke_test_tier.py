"""End-to-end smoke test against the test tier: real PDFs through all 3 products.

Health checks only prove the process is up. This proves the deployed image
actually OCRs, splits and extracts — the thing v20260824a changed the worker's
Redis handling underneath.

Key handling: the test-tier API key is read from the env, or from a file named
by KEY_FILE. It is never printed, and never written anywhere by this script.

Usage:
    KEY_FILE=/tmp/testkey python smoke_test_tier.py            # 1 PDF/product
    KEY_FILE=/tmp/testkey N=3 python smoke_test_tier.py        # 3 PDFs/product
    KEY_FILE=/tmp/testkey PRODUCTS=bps python smoke_test_tier.py
"""
import json
import os
import pathlib
import sys
import time

import requests

REPO = pathlib.Path(__file__).resolve().parent
CORPUS = pathlib.Path(os.getenv("CORPUS", "test_uploads"))
N = int(os.getenv("N", "1"))
JOB_TIMEOUT_S = int(os.getenv("JOB_TIMEOUT_S", "420"))
POLL_S = 10

PRODUCTS = (os.getenv("PRODUCTS") or "vetcostcheck,bps,sanierer").split(",")


KEY_DIR = pathlib.Path(os.getenv("KEY_DIR", "/tmp"))


def api_key(product):
    """Each product has its own INVOICE_API_KEY — a bps key 401s on sanierer.

    Looked up as KEY_DIR/key-<product>, then KEY_FILE, then the env. Values are
    never printed and never written by this script.
    """
    per_product = KEY_DIR / f"key-{product}"
    if per_product.exists():
        k = per_product.read_text().strip()
        if k:
            return k
    kf = os.getenv("KEY_FILE")
    if kf and pathlib.Path(kf).exists():
        k = pathlib.Path(kf).read_text().strip()
        if k:
            return k
    return os.getenv("INVOICE_API_KEY", "").strip()


def headers(product):
    return {"X-API-Key": api_key(product)}


def base_url(product):
    return os.getenv("API_BASE") or f"https://3c{product}-test.flex-capital-scale.com"


def pick_pdfs(product, n):
    d = CORPUS / product
    if not d.is_dir():
        return []
    pdfs = sorted(p for p in d.rglob("*.pdf") if p.is_file())
    return pdfs[:n]


def run_one(product, pdf):
    """Returns (ok, detail). Never raises."""
    base = base_url(product)
    HEADERS = headers(product)
    t0 = time.time()
    try:
        with open(pdf, "rb") as f:
            r = requests.post(f"{base}/upload", files={"file": f}, headers=HEADERS, timeout=180)
        if r.status_code != 200:
            return False, f"upload HTTP {r.status_code}"
        file_id = r.json()["file_id"]

        r = requests.post(f"{base}/process", json={"file_id": file_id},
                          headers=HEADERS, timeout=60)
        if r.status_code != 200:
            return False, f"process HTTP {r.status_code}"
        job_id = r.json()["job_id"]

        deadline = time.time() + JOB_TIMEOUT_S
        last = "?"
        while time.time() < deadline:
            r = requests.get(f"{base}/job/{job_id}", headers=HEADERS, timeout=60)
            if r.status_code != 200:
                return False, f"poll HTTP {r.status_code}"
            data = r.json()
            last = data.get("status", "?")
            if last == "finished":
                return validate(data.get("result"), time.time() - t0)
            if last == "failed":
                return False, f"job failed: {str(data.get('error'))[:200]}"
            time.sleep(POLL_S)
        return False, f"timeout after {JOB_TIMEOUT_S}s (last status {last})"
    except Exception as e:  # noqa: BLE001 — a smoke test reports, never crashes
        return False, f"{type(e).__name__}: {str(e)[:160]}"


def validate(result, elapsed):
    """The job said finished; check the payload actually looks like an extraction."""
    if result is None:
        return False, "finished but result was null"
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return False, f"result is a non-JSON string ({len(result)} chars)"
    if isinstance(result, list):
        subdocs = result
    elif isinstance(result, dict):
        for key in ("subdocuments", "invoices", "results", "documents"):
            if isinstance(result.get(key), list):
                subdocs = result[key]
                break
        else:
            subdocs = [result]
    else:
        return False, f"unexpected result type {type(result).__name__}"

    if not subdocs:
        return False, "finished with zero subdocuments"

    keys = sorted({k for d in subdocs if isinstance(d, dict) for k in d})
    return True, f"{len(subdocs)} subdoc(s) in {elapsed:.0f}s; fields: {len(keys)}"


def main():
    print(f"corpus={CORPUS}  n_per_product={N}  timeout={JOB_TIMEOUT_S}s")
    print("keys: " + ", ".join(f"{p.strip()}={len(api_key(p.strip()))}ch" for p in PRODUCTS) + " (values not shown)\n")
    rows, failures = [], 0
    for product in PRODUCTS:
        product = product.strip()
        if not api_key(product):
            rows.append((product, "-", False, f"no API key (expected {KEY_DIR}/key-{product})"))
            failures += 1
            continue
        pdfs = pick_pdfs(product, N)
        if not pdfs:
            rows.append((product, "-", False, f"no PDFs under {CORPUS/product}"))
            failures += 1
            continue
        for pdf in pdfs:
            print(f"→ {product}: {pdf.name} ...", flush=True)
            ok, detail = run_one(product, pdf)
            print(f"  {'PASS' if ok else 'FAIL'}: {detail}\n", flush=True)
            rows.append((product, pdf.name, ok, detail))
            if not ok:
                failures += 1

    print("=" * 78)
    print(f"{'product':14s} {'file':30s} {'ok':5s} detail")
    print("-" * 78)
    for product, name, ok, detail in rows:
        print(f"{product:14s} {name[:30]:30s} {'PASS' if ok else 'FAIL':5s} {detail[:60]}")
    print("=" * 78)
    print(f"{len(rows) - failures}/{len(rows)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
