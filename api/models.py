from pydantic import BaseModel, Field


# --- Extraction result models ---

class SenderModel(BaseModel):
    practiceName: str | None = None
    address: str | None = None
    postcode: str | None = None
    city: str | None = None
    country: str | None = None
    contactPhone: str | None = None
    contactMail: str | None = None
    vatId: str | None = None


class ClinicianModel(BaseModel):
    name: str | None = None
    title: str | None = None


class PaymentModel(BaseModel):
    iban: str | None = None
    bic: str | None = None
    bankName: str | None = None
    dueDate: str | None = Field(None, description="YYYY-MM-DD")


class RecipientModel(BaseModel):
    companyName: str | None = None
    contactFirstname: str | None = None
    contactName: str | None = None
    street: str | None = None
    postcode: str | None = None
    city: str | None = None
    country: str | None = None
    contactPhone: str | None = None
    contactMail: str | None = None


class AnimalModel(BaseModel):
    name: str | None = None
    species: str | None = None
    breed: str | None = None


class GOTModel(BaseModel):
    code: str | None = Field(None, description="GOT code (1-4 digits)")
    multiplier: float | None = None
    raw: str | None = Field(None, description="Original GOT string from invoice")


class ItemAnimalModel(BaseModel):
    name: str | None = None
    species: str | None = None


class SourceModel(BaseModel):
    ids: list[str] | None = Field(None, description="OCR reference IDs")
    snippet: str | None = Field(None, description="Text excerpt from invoice")


class LineItemModel(BaseModel):
    name: str | None = None
    got: GOTModel | None = None
    animal: ItemAnimalModel | None = None
    qty: float | None = None
    unit: str | None = None
    unitPriceNet: float | None = None
    lineTotalNet: float | None = None
    serviceDate: str | None = Field(None, description="YYYY-MM-DD")
    source: SourceModel | None = None


class TaxModel(BaseModel):
    rate: float | None = Field(None, description="Tax rate percentage, e.g. 19.0")
    amount: float | None = None


class TotalsModel(BaseModel):
    net: float | None = None
    tax: TaxModel | None = None
    gross: float | None = None
    discount: float | None = None


class SubdocumentModel(BaseModel):
    type: str | None = Field(None, description="invoice, receipt, or null")
    currency: str | None = Field(None, description="ISO 4217, e.g. EUR")
    number: str | None = None
    issuedAt: str | None = Field(None, description="YYYY-MM-DD")
    serviceDates: list[str] | None = None
    sender: SenderModel | None = None
    clinicians: list[ClinicianModel] | None = None
    payment: PaymentModel | None = None
    recipient: RecipientModel | None = None
    animals: list[AnimalModel] | None = None
    diagnoses: list[str] | None = None
    items: list[LineItemModel] | None = None
    totals: TotalsModel | None = None
    warnings: list[str] | None = None


class ExtractionResultModel(BaseModel):
    number_of_subdocuments: int | None = None
    subdocuments: list[SubdocumentModel] | None = None


# --- API request/response models ---

class ProcessRequest(BaseModel):
    file_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: ExtractionResultModel | None = None
    error: str | None = None
