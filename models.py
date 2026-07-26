"""All Pydantic request/response schemas for Steno Desk."""
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- auth -----
class UserPublic(StrictModel):
    id: str
    email: EmailStr
    name: Optional[str] = None
    cert_number: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    trial_started_at: Optional[str] = None
    trial_ends_at: Optional[str] = None
    created_at: str


class SignupIn(StrictModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: Optional[str] = None
    beta: Optional[bool] = False  # set true from /signup?beta=1 to grant 60-day trial


class LoginIn(StrictModel):
    email: EmailStr
    password: str


class ForgotIn(StrictModel):
    email: EmailStr


class ResetIn(StrictModel):
    token: str
    new_password: str = Field(min_length=8)


class SettingsIn(StrictModel):
    name: Optional[str] = None
    cert_number: Optional[str] = None
    cert_type: Optional[str] = None
    business_name: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    default_net_days: Optional[int] = None
    invoice_prefix: Optional[str] = None
    payment_instructions_default: Optional[str] = None
    # Notifications & automation
    auto_reminders_enabled: Optional[bool] = None
    notify_on_open: Optional[bool] = None


# ---------------------------------------------------------------- clients --
class Rates(StrictModel):
    original_per_page: Optional[float] = None
    copy_per_page: Optional[float] = None
    appearance_fee: Optional[float] = None
    appearance_hourly: Optional[float] = None
    rough_draft_per_page: Optional[float] = None
    rough_draft_flat: Optional[float] = None
    realtime_fee: Optional[float] = None
    read_sign_fee: Optional[float] = None


class ClientIn(StrictModel):
    name: str
    type: Literal["Agency", "Law Firm", "Direct", "Other"] = "Agency"
    contact_name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    billing_address: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    rates: Rates = Field(default_factory=Rates)


class ClientOut(ClientIn):
    id: str
    created_at: str
    job_count: int = 0
    last_job_date: Optional[str] = None


class AttorneyIn(StrictModel):
    first_name: str
    last_name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    client_id: Optional[str] = None
    notes: Optional[str] = None


class AttorneyOut(AttorneyIn):
    id: str
    created_at: str


# ---------------------------------------------------------------- jobs ----
class JobIn(StrictModel):
    case_caption: Optional[str] = None
    case_number: Optional[str] = None
    witness: str
    job_date: str  # ISO date
    start_time: Optional[str] = None
    location: Optional[str] = None
    job_type: Literal["Deposition", "EBT", "Arbitration", "Hearing", "Other"] = "Deposition"
    client_id: str
    ordering_attorney_id: Optional[str] = None
    ordering_attorney_text: Optional[str] = None
    opposing_attorney_id: Optional[str] = None
    opposing_attorney_text: Optional[str] = None
    status: Literal["Scheduled", "Completed", "Invoiced", "Paid"] = "Scheduled"
    notes: Optional[str] = None
    # Scopist assignment (optional). `scopist_status` is set by the scopist
    # portal — the reporter assigns by setting `scopist_id`.
    scopist_id: Optional[str] = None
    scopist_status: Optional[Literal["Assigned", "In Progress", "Completed"]] = None


class JobOut(JobIn):
    id: str
    created_at: str
    invoice_id: Optional[str] = None
    scoping_completed_at: Optional[str] = None


# ---------------------------------------------------------------- scopists --
class ScopistIn(StrictModel):
    first_name: str
    last_name: str
    email: Optional[EmailStr] = None
    rate_per_page: Optional[float] = None
    notes: Optional[str] = None


class ScopistOut(ScopistIn):
    id: str
    created_at: str
    share_token: str
    open_jobs: int = 0


# ---------------------------------------------------------------- templates -
class InvoiceTemplateIn(StrictModel):
    name: str
    client_id: Optional[str] = None
    line_items: List["LineItem"] = []
    notes: Optional[str] = None
    payment_instructions: Optional[str] = None


class InvoiceTemplateOut(InvoiceTemplateIn):
    id: str
    created_at: str


# ---------------------------------------------------------------- recurring -
class RecurringIn(StrictModel):
    name: str
    client_id: str
    frequency: Literal["weekly", "monthly"] = "monthly"
    day_of_month: Optional[int] = Field(default=1, ge=1, le=28)  # cap at 28 to avoid Feb edge cases
    day_of_week: Optional[int] = Field(default=1, ge=0, le=6)    # 0=Mon .. 6=Sun
    next_run_date: str  # ISO date — first run on/after this date
    line_items: List["LineItem"] = []
    notes: Optional[str] = None
    payment_instructions: Optional[str] = None
    active: bool = True


class RecurringOut(RecurringIn):
    id: str
    created_at: str
    last_run_at: Optional[str] = None
    last_invoice_id: Optional[str] = None
    runs_count: int = 0


# ---------------------------------------------------------------- invoices
class LineItem(StrictModel):
    type: str  # appearance_fee | original_transcript | copy | rough_draft | realtime | expedite | read_sign | exhibits | mileage | scopist_deduction | late_delivery | custom
    label: str
    detail: Optional[str] = None
    quantity: Optional[float] = None
    rate: Optional[float] = None
    amount: float


class InvoiceIn(StrictModel):
    job_id: Optional[str] = None
    client_id: str
    invoice_date: str
    due_date: str
    line_items: List[LineItem] = []
    notes: Optional[str] = None
    payment_instructions: Optional[str] = None


class InvoiceOut(InvoiceIn):
    id: str
    invoice_number: str
    status: Literal["Draft", "Sent", "Paid", "Void"]
    total: float
    billed_to_name: Optional[str] = None
    billed_to_email: Optional[str] = None
    billed_to_address: Optional[str] = None
    created_at: str
    sent_at: Optional[str] = None
    paid_at: Optional[str] = None
    voided_at: Optional[str] = None
    # Postmark delivery tracking — populated by /api/webhooks/postmark
    message_id: Optional[str] = None
    delivered_at: Optional[str] = None
    opened_at: Optional[str] = None
    last_opened_at: Optional[str] = None
    opens_count: Optional[int] = 0
    bounce_status: Optional[str] = None
    bounce_at: Optional[str] = None
    bounce_message: Optional[str] = None


class SendInvoiceIn(StrictModel):
    to_email: EmailStr
    cc: Optional[EmailStr] = None
    subject: str
    body: str


# ---------------------------------------------------------------- bulk ----
class BulkGenerateIn(StrictModel):
    job_ids: List[str]


class BulkSendIn(StrictModel):
    invoice_ids: List[str]
    subject_prefix: Optional[str] = None  # optional prefix prepended to each subject


class PaymentIn(StrictModel):
    amount: float = Field(gt=0)
    payment_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    payment_method: Literal["check", "ach", "wire", "cash", "other"] = "check"
    reference: Optional[str] = None
    notes: Optional[str] = None


class PaymentOut(PaymentIn):
    id: str
    invoice_id: str
    created_at: str


# ---------------------------------------------------------------- expenses
class ExpenseIn(StrictModel):
    date: str
    amount: float
    description: str
    category: Literal[
        "Scopist", "Mileage", "Software", "Continuing Education",
        "Supplies", "Equipment", "Professional Dues", "Other",
    ]
    miles: Optional[float] = None
    irs_rate: Optional[float] = None
    receipt_url: Optional[str] = None
    receipt_path: Optional[str] = None
    receipt_content_type: Optional[str] = None
    notes: Optional[str] = None


class ExpenseOut(ExpenseIn):
    id: str
    created_at: str


# ---------------------------------------------------------------- leads ---
class LeadIn(StrictModel):
    email: EmailStr
    source: Optional[str] = None


# ---------------------------------------------------------------- portal ----
class PortalShareIn(StrictModel):
    """Request body for emailing an invoice share link to the client."""
    to_email: Optional[EmailStr] = None
    subject: Optional[str] = None
    body: Optional[str] = None


# Resolve forward refs for templates / recurring (which reference LineItem
# defined further down).
InvoiceTemplateIn.model_rebuild()
InvoiceTemplateOut.model_rebuild()
RecurringIn.model_rebuild()
RecurringOut.model_rebuild()
