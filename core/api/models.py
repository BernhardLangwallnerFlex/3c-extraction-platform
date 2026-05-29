from pydantic import BaseModel


# --- API request/response models ---
#
# The extraction result is PRODUCT-SPECIFIC: each product defines its own output
# schema in products/<name>/extract_schema.json. The API must NOT coerce the
# result to a fixed shape. A previous vet-only response model (SubdocumentModel /
# LineItemModel) silently stripped every non-vet field from the API response —
# e.g. BPS/Sanierer `position`, `lvPosition`, `unitCode`, `taxRate`, `discount`
# and BPS `serviceProvider` / `policyholder` / `damageLocation`. So `result` is
# passed through verbatim as a dict; the per-product schema is the contract.


class ProcessRequest(BaseModel):
    file_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: dict | None = None
    error: str | None = None
