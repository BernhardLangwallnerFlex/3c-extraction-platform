#!/usr/bin/env python3
"""
Load test for the VetCostCheck Invoice Extraction API.

Uploads N PDFs, triggers processing, polls until all complete, reports throughput.

Usage:
    python load_test.py                  # 3 docs (default)
    python load_test.py --num-docs 5     # 5 docs
    python load_test.py --num-docs 10 --timeout 3600  # 10 docs, 1h timeout
"""

import argparse
import os
import random
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("API_URL", "https://3cvetcostcheck.flex-capital-scale.com")
API_KEY = os.getenv("INVOICE_API_KEY", "")
PDF_DIR = Path("3C_testdaten_pdf")
POLL_INTERVAL = 15  # seconds


def get_pdf_files():
    return sorted(p for p in PDF_DIR.glob("*.pdf") if p.is_file())


def upload(pdf_path):
    with open(pdf_path, "rb") as f:
        resp = requests.post(
            f"{API_URL}/upload",
            headers={"X-Api-Key": API_KEY},
            files={"file": (pdf_path.name, f, "application/pdf")},
            timeout=60,
        )
    resp.raise_for_status()
    return resp.json()["file_id"]


def trigger_processing(file_id):
    resp = requests.post(
        f"{API_URL}/process",
        headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
        json={"file_id": file_id},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["job_id"]


def poll_job(job_id):
    resp = requests.get(
        f"{API_URL}/job/{job_id}",
        headers={"X-Api-Key": API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def run_load_test(num_docs, timeout):
    pdfs = get_pdf_files()
    if not pdfs:
        print(f"No PDFs found in {PDF_DIR}")
        return

    # Sample with replacement if we need more than available
    selected = [random.choice(pdfs) for _ in range(num_docs)]
    print(f"Selected {num_docs} PDFs from {len(pdfs)} available")
    for p in selected:
        print(f"  - {p.name}")
    print()

    # Phase 1: Upload all PDFs (parallel)
    print("=== Phase 1: Uploading ===")
    t_upload_start = time.monotonic()
    jobs = []

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(upload, pdf): pdf for pdf in selected}
        for future in futures:
            pdf = futures[future]
            try:
                file_id = future.result()
                print(f"  Uploaded {pdf.name} -> {file_id}")
                jobs.append({"pdf": pdf.name, "file_id": file_id})
            except Exception as e:
                print(f"  FAILED to upload {pdf.name}: {e}")

    t_upload = time.monotonic() - t_upload_start
    print(f"  Uploads done in {t_upload:.1f}s")
    print()

    # Phase 2: Trigger processing (parallel)
    print("=== Phase 2: Triggering processing ===")
    t_process_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(trigger_processing, j["file_id"]): j for j in jobs}
        for future in futures:
            job = futures[future]
            try:
                job_id = future.result()
                job["job_id"] = job_id
                job["start_time"] = time.monotonic()
                print(f"  Queued {job['pdf']} -> job {job_id[:12]}...")
            except Exception as e:
                print(f"  FAILED to process {job['pdf']}: {e}")
                job["job_id"] = None

    jobs = [j for j in jobs if j.get("job_id")]
    print(f"  {len(jobs)} jobs queued")
    print()

    # Phase 3: Poll until all complete or timeout
    print("=== Phase 3: Polling for results ===")
    pending = {j["job_id"]: j for j in jobs}
    completed = []
    failed = []
    deadline = time.monotonic() + timeout

    while pending and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        elapsed = time.monotonic() - t_process_start

        done_this_round = []
        for job_id, job in pending.items():
            try:
                result = poll_job(job_id)
                status = result["status"]

                if status == "finished":
                    job["end_time"] = time.monotonic()
                    job["duration"] = job["end_time"] - job["start_time"]
                    subdocs = result.get("result", {}).get("number_of_subdocuments", "?")
                    job["subdocuments"] = subdocs
                    completed.append(job)
                    done_this_round.append(job_id)
                    print(f"  [{elapsed:5.0f}s] {job['pdf']}: finished ({subdocs} subdocs, {job['duration']:.0f}s)")

                elif status == "failed":
                    job["end_time"] = time.monotonic()
                    job["duration"] = job["end_time"] - job["start_time"]
                    job["error"] = result.get("error", "unknown")
                    failed.append(job)
                    done_this_round.append(job_id)
                    print(f"  [{elapsed:5.0f}s] {job['pdf']}: FAILED ({job['error']})")

            except Exception as e:
                print(f"  [{elapsed:5.0f}s] Error polling {job_id[:12]}: {e}")

        for jid in done_this_round:
            del pending[jid]

        if pending:
            print(f"  [{elapsed:5.0f}s] {len(pending)} jobs still running...")

    timed_out = list(pending.values())
    total_time = time.monotonic() - t_process_start

    # Report
    print()
    print("=" * 60)
    print("LOAD TEST RESULTS")
    print("=" * 60)
    print(f"  Documents submitted:   {num_docs}")
    print(f"  Completed:             {len(completed)}")
    print(f"  Failed:                {len(failed)}")
    print(f"  Timed out:             {len(timed_out)}")
    print(f"  Upload phase:          {t_upload:.1f}s")
    print(f"  Processing phase:      {total_time:.1f}s")
    print()

    if completed:
        durations = [j["duration"] for j in completed]
        print(f"  Min job duration:      {min(durations):.0f}s")
        print(f"  Max job duration:      {max(durations):.0f}s")
        print(f"  Avg job duration:      {sum(durations) / len(durations):.0f}s")
        print(f"  Throughput:            {len(completed) / (total_time / 60):.2f} docs/min")
        print()

        print("  Per-job breakdown:")
        for j in sorted(completed, key=lambda x: x["duration"]):
            print(f"    {j['pdf']:40s} {j['duration']:6.0f}s  ({j['subdocuments']} subdocs)")

    if failed:
        print()
        print("  Failures:")
        for j in failed:
            print(f"    {j['pdf']:40s} {j['error']}")

    if timed_out:
        print()
        print("  Timed out (still running):")
        for j in timed_out:
            print(f"    {j['pdf']}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VetCostCheck API load test")
    parser.add_argument("--num-docs", type=int, default=3, help="Number of PDFs to process (default: 3)")
    parser.add_argument("--timeout", type=int, default=1800, help="Max wait time in seconds (default: 1800)")
    args = parser.parse_args()

    run_load_test(args.num_docs, args.timeout)
