# VetCostCheck Invoice Extraction API

## Overview

The VetCostCheck API extracts structured data from veterinary invoices (PDF). Upload a PDF, trigger processing, and poll for results. The system handles multi-invoice PDFs — a single file containing multiple invoices will be automatically split and each invoice extracted separately.

**Base URL:** `https://3cvetcostcheck.flex-capital-scale.com`

**Authentication:** All requests require the `X-Api-Key` header.

---

## Environments

| Environment | Base URL |
|---|---|
| Production | `https://3cvetcostcheck.flex-capital-scale.com` |
| Test | `https://3cvetcostcheck-test.flex-capital-scale.com` |

Both environments expose an identical API. Use **Test** to validate against a change before
it reaches production — every change is deployed there first and only promoted to production
once verified.

Three things to know about Test:

- **It uses a separate API key.** Your production key will be rejected, and vice versa.
- **It is fully isolated.** Separate job queue and separate storage, so test uploads never
  appear in production and never consume production processing capacity.
- **The first request after an idle period is slow.** Test runs with no always-on instance to
  keep it cheap, so expect up to a minute on a cold start. Subsequent requests are normal
  speed. This is expected behaviour, not a fault.

All examples below use the production URL; substitute the test URL and its key to run them
against Test.

---

## Workflow

The API uses an asynchronous processing model with three steps:

```
1. Upload PDF  →  returns file_id
2. Start processing  →  returns job_id
3. Poll for result  →  returns extracted data (JSON)
```

---

## Step 1 — Upload a PDF

Upload the invoice PDF file. The API returns a `file_id` that you'll use in the next step.

```bash
curl -X POST https://3cvetcostcheck.flex-capital-scale.com/upload \
  -H "X-Api-Key: <YOUR_API_KEY>" \
  -F "file=@invoice.pdf"
```

**Response:**

```json
{
  "file_id": "a3b1c9d4-7e82-4f10-b6a3-1234abcd5678.pdf"
}
```

---

## Step 2 — Start Processing

Trigger the extraction pipeline for the uploaded file. This runs OCR, document analysis, and data extraction in the background. The API returns a `job_id` for tracking.

```bash
curl -X POST https://3cvetcostcheck.flex-capital-scale.com/process \
  -H "X-Api-Key: <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"file_id": "a3b1c9d4-7e82-4f10-b6a3-1234abcd5678.pdf"}'
```

**Response:**

```json
{
  "job_id": "fe9c2001-44dc-4254-a0f7-18ff46eaca42",
  "status": "queued",
  "queue": "invoice-jobs"
}
```

---

## Step 3 — Poll for Results

Check the job status. Processing typically takes 2–5 minutes depending on the number of pages and invoices in the PDF. Poll this endpoint periodically (e.g., every 10–15 seconds) until the status is `finished` or `failed`.

```bash
curl https://3cvetcostcheck.flex-capital-scale.com/job/fe9c2001-44dc-4254-a0f7-18ff46eaca42 \
  -H "X-Api-Key: <YOUR_API_KEY>"
```

### While processing:

```json
{
  "job_id": "fe9c2001-44dc-4254-a0f7-18ff46eaca42",
  "status": "started"
}
```

### When finished:

```json
{
  "job_id": "fe9c2001-44dc-4254-a0f7-18ff46eaca42",
  "status": "finished",
  "result": {
    "number_of_subdocuments": 3,
    "subdocuments": [
      {
        "returncode": 100,
        "returncodeReasons": [],
        "type": "invoice",
        "number": "28679/23-002307RP",
        "date": "2023-10-15",
        "sender": { ... },
        "recipient": { ... },
        "animals": [ ... ],
        "line_items": [ ... ],
        "totals": {
          "net": 2189.03,
          "vat": 415.91,
          "gross": 2604.94
        }
      },
      ...
    ]
  }
}
```

### If processing fails:

```json
{
  "job_id": "fe9c2001-44dc-4254-a0f7-18ff46eaca42",
  "status": "failed",
  "error": "Error description"
}
```

---

## Job Status Values

| Status | Meaning |
|--------|---------|
| `queued` | Job is waiting to be picked up |
| `started` | Job is actively being processed |
| `finished` | Extraction complete — result is in the response |
| `failed` | Something went wrong — check the `error` field |

---

## Subdocument Return Codes

Every entry in `subdocuments` carries a `returncode`. It is **always present**
and is **always** one of the three values below — branch on it rather than on
whether individual fields came back null.

| Code | Meaning | What to do |
|------|---------|------------|
| `100` | The subdocument is an invoice, receipt or prescription | Process it normally |
| `200` | Readable, but not an invoice — a cover letter, e-mail, photo, privacy notice, … | Do not open a Vorgang for it |
| `300` | The content could not be read at all | Needs a human |

`returncodeReasons` is a list of short German sentences explaining a `200` or
`300`. It is empty for `100`, and non-empty whenever the code is `200` or
`300`, so it can be pasted straight into a Storno note.

Two things worth knowing:

- **A `100` does not promise complete extraction.** A genuine invoice stays
  `100` even when individual fields could not be read — those caveats go in
  `warnings`, which keeps its existing meaning and is separate from
  `returncodeReasons`.
- **`subdocuments` is never empty.** A PDF that contains no invoice at all
  returns exactly one subdocument with `returncode: 200` rather than an empty
  array, so there is always a code to read.

---

## Health Check

To verify the API is running (no authentication required):

```bash
curl https://3cvetcostcheck.flex-capital-scale.com/healthz
```

```json
{"status": "ok"}
```

---

## Notes

- **File format:** Only PDF files are supported.
- **Multi-invoice PDFs:** The system automatically detects and splits PDFs that contain multiple invoices.
- **Processing time:** Typically 2–5 minutes per file. Larger PDFs with many pages may take longer.
- **Result retention:** Job results are available for 1 hour after completion. After that, the job ID expires.
- **Rate limits:** No hard rate limit, but processing is sequential per worker. Multiple uploads can be queued and will be processed in order.
