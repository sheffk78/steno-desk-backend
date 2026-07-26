"""Postmark email service for Steno Desk."""
import base64
import logging
import os
from typing import Optional, Tuple

from postmarker.core import PostmarkClient

logger = logging.getLogger(__name__)


def _client() -> PostmarkClient:
    token = os.environ.get("POSTMARK_SERVER_TOKEN", "")
    return PostmarkClient(server_token=token)


def _sender() -> str:
    name = os.environ.get("SENDER_NAME", "Steno Desk")
    addr = os.environ.get("SENDER_EMAIL", "no-reply@contact.stenodesk.co")
    return f"{name} <{addr}>"


def send_password_reset_email(
    to_email: str,
    reset_link: str,
    user_name: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    display = user_name or "there"
    expire_min = os.environ.get("RESET_TOKEN_EXPIRE_MINUTES", "60")
    html = f"""
    <html><body style="font-family: -apple-system, 'IBM Plex Sans', sans-serif; line-height:1.6; color:#1C1917; background:#FAFAF9; padding:24px;">
      <div style="max-width:560px; margin:0 auto; background:#fff; border:1px solid #E7E5E4; padding:32px;">
        <h1 style="font-family: 'Cormorant Garamond', Georgia, serif; font-size:28px; margin:0 0 16px 0; color:#1E293B;">Reset your password</h1>
        <p>Hello {display},</p>
        <p>We received a request to reset the password on your Steno Desk account. If you didn't request this, you can ignore this email.</p>
        <p>This link expires in {expire_min} minutes.</p>
        <p style="margin:24px 0;">
          <a href="{reset_link}" style="display:inline-block; background:#1E293B; color:#fff; padding:10px 18px; text-decoration:none; border-radius:6px; font-weight:500;">Reset password</a>
        </p>
        <p style="font-size:13px; color:#78716C; word-break:break-all;">Or paste this link into your browser:<br>{reset_link}</p>
        <hr style="margin:24px 0; border:none; border-top:1px solid #E7E5E4;">
        <p style="font-size:12px; color:#78716C;">Steno Desk — practice management for freelance court reporters.</p>
      </div>
    </body></html>
    """
    text = f"Hello {display},\n\nReset your password: {reset_link}\nThis link expires in {expire_min} minutes.\n\nSteno Desk"
    try:
        resp = _client().emails.send(
            From=_sender(),
            To=to_email,
            Subject="Reset your Steno Desk password",
            HtmlBody=html,
            TextBody=text,
            MessageStream="outbound",
        )
        return True, resp.get("MessageID")
    except Exception as e:
        logger.error(f"Postmark password reset failed for {to_email}: {e}")
        return False, None


def send_invoice_email(
    to_email: str,
    subject: str,
    body_text: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    cc: Optional[str] = None,
    reporter_name: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    attachment = {
        "Name": pdf_filename,
        "Content": pdf_b64,
        "ContentType": "application/pdf",
    }

    sig = reporter_name or "Steno Desk"
    body_html = "<html><body style=\"font-family:-apple-system,'IBM Plex Sans',sans-serif; color:#1C1917; line-height:1.6;\">"
    body_html += "<div style='max-width:560px;'>"
    for para in body_text.strip().split("\n\n"):
        body_html += f"<p>{para.replace(chr(10), '<br>')}</p>"
    body_html += f"<p style='margin-top:24px;'>—<br>{sig}</p>"
    body_html += "</div></body></html>"

    payload = dict(
        From=_sender(),
        To=to_email,
        Subject=subject,
        HtmlBody=body_html,
        TextBody=body_text + f"\n\n—\n{sig}",
        Attachments=[attachment],
        MessageStream="outbound",
        TrackOpens=True,
    )
    if cc:
        payload["Cc"] = cc

    try:
        resp = _client().emails.send(**payload)
        return True, resp.get("MessageID")
    except Exception as e:
        logger.error(f"Postmark invoice send failed for {to_email}: {e}")
        return False, None


def send_overdue_reminder_email(
    user: dict,
    invoice: dict,
    days_overdue: int,
    reminder_number: int,
    pdf_bytes: bytes,
) -> Tuple[bool, Optional[str]]:
    """Polite "just following up" email with the PDF re-attached.
    Tone escalates very slightly with reminder_number (1st = friendly,
    2nd = direct, 3rd = firm-but-still-professional)."""
    to_email = (invoice.get("billed_to_email") or "").strip()
    if not to_email:
        return False, None

    inv_no = invoice.get("invoice_number") or invoice.get("id", "")[:8]
    amount = invoice.get("total") or 0.0
    due_date = invoice.get("due_date") or ""
    bill_to = invoice.get("billed_to_name") or "there"
    reporter = user.get("name") or user.get("business_name") or "Steno Desk"

    if reminder_number == 1:
        subject = f"Friendly reminder: Invoice {inv_no} (${amount:,.2f})"
        opener = (f"Hi {bill_to}, just a friendly reminder that invoice {inv_no} "
                  f"for ${amount:,.2f} was due on {due_date} — about {days_overdue} "
                  f"days ago. The invoice is re-attached for your convenience.")
    elif reminder_number == 2:
        subject = f"Second reminder: Invoice {inv_no} is past due (${amount:,.2f})"
        opener = (f"Hi {bill_to}, following up on invoice {inv_no} for ${amount:,.2f}, "
                  f"which is now {days_overdue} days past due. Please let me know "
                  f"if there's anything I can help clarify so we can get this closed out.")
    else:
        subject = f"Past due: Invoice {inv_no} (${amount:,.2f}) — {days_overdue} days"
        opener = (f"Hi {bill_to}, invoice {inv_no} for ${amount:,.2f} is now "
                  f"{days_overdue} days past due. Please let me know how you'd like "
                  f"to settle this — happy to discuss payment options if helpful.")

    closing = ("If you've already sent payment, please disregard this note and accept "
               "my thanks. Otherwise, I'd appreciate hearing back when I can expect payment.")
    body_text = f"{opener}\n\n{closing}"

    body_html = (
        "<html><body style=\"font-family:-apple-system,'IBM Plex Sans',sans-serif; "
        "color:#1C1917; line-height:1.6;\"><div style='max-width:560px;'>"
        f"<p>{opener}</p><p>{closing}</p>"
        f"<p style='margin-top:24px;'>—<br>{reporter}</p>"
        "</div></body></html>"
    )

    pdf_filename = f"Invoice-{inv_no}.pdf"
    attachment = {
        "Name": pdf_filename,
        "Content": base64.b64encode(pdf_bytes).decode("utf-8"),
        "ContentType": "application/pdf",
    }
    try:
        resp = _client().emails.send(
            From=_sender(),
            To=to_email,
            Subject=subject,
            HtmlBody=body_html,
            TextBody=body_text + f"\n\n—\n{reporter}",
            Attachments=[attachment],
            MessageStream="outbound",
            TrackOpens=True,
        )
        return True, resp.get("MessageID")
    except Exception as e:
        logger.error(f"Postmark overdue reminder failed for {to_email}: {e}")
        return False, None


def send_invoice_opened_notification(
    to_email: str,
    reporter_name: str,
    client_name: str,
    invoice_number: str,
    invoice_total: float,
    app_invoice_url: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Tell the reporter that their client opened the invoice — fires
    once per invoice (on the first Postmark Open webhook)."""
    display = reporter_name or "there"
    subject = f"{client_name} just opened invoice {invoice_number}"
    cta = ""
    if app_invoice_url:
        cta = (f"<p style='margin-top:18px;'><a href='{app_invoice_url}' "
               f"style='background:#1F2937; color:#fff; padding:10px 18px; "
               f"border-radius:6px; text-decoration:none; display:inline-block;'>"
               f"View invoice</a></p>")
    html = f"""
    <html><body style="font-family:-apple-system,'IBM Plex Sans',sans-serif; color:#1C1917;">
      <div style="max-width:540px; line-height:1.6;">
        <p>Hi {display},</p>
        <p><strong>{client_name}</strong> just opened invoice
          <strong>{invoice_number}</strong> for <strong>${invoice_total:,.2f}</strong>.</p>
        <p>This might be a good moment to follow up if payment is overdue, or
          simply nice to know the email made it through.</p>
        {cta}
        <hr style="margin:24px 0; border:none; border-top:1px solid #E7E5E4;">
        <p style="font-size:12px; color:#78716C;">You're receiving this because
          you have invoice-open notifications enabled. Turn them off any time
          in Settings → Profile.</p>
      </div>
    </body></html>
    """
    text = (f"Hi {display},\n\n{client_name} just opened invoice {invoice_number} "
            f"for ${invoice_total:,.2f}.\n\n"
            f"{('View it in Steno Desk: ' + app_invoice_url) if app_invoice_url else ''}\n\n"
            "— Steno Desk\n\n"
            "Turn off these notifications in Settings → Profile.")
    try:
        resp = _client().emails.send(
            From=_sender(),
            To=to_email,
            Subject=subject,
            HtmlBody=html,
            TextBody=text,
            MessageStream="outbound",
        )
        return True, resp.get("MessageID")
    except Exception as e:
        logger.error(f"Postmark open-notification failed for {to_email}: {e}")
        return False, None



def _admin_notify_addr() -> Optional[str]:
    """First entry in ADMIN_EMAILS, or the hardcoded founder email if the
    env var isn't set. This means admin notifications go somewhere even on
    a fresh deploy where ADMIN_EMAILS hasn't been configured yet."""
    raw = os.environ.get("ADMIN_EMAILS", "").strip()
    if raw:
        first = raw.split(",")[0].strip().strip('"').strip("'")
        if first:
            return first
    # Fallback — keep in sync with FOUNDER_EMAIL in auth_core.py.
    return "support@stenodesk.co"


def send_admin_notification(subject: str, body_html: str, body_text: str) -> Tuple[bool, Optional[str]]:
    """Generic internal-only email to the support inbox (first address in
    ADMIN_EMAILS, currently support@stenodesk.co). Used for "new signup",
    "new lead", etc. Silently no-ops if ADMIN_EMAILS is unset so dev
    environments aren't noisy."""
    to = _admin_notify_addr()
    if not to:
        return False, None
    try:
        resp = _client().emails.send(
            From=_sender(),
            To=to,
            Subject=subject,
            HtmlBody=body_html,
            TextBody=body_text,
            MessageStream="outbound",
        )
        return True, resp.get("MessageID")
    except Exception as e:
        logger.error(f"admin notification failed → {to}: {e}")
        return False, None


def send_new_signup_notification(user: dict) -> Tuple[bool, Optional[str]]:
    """Fired from /auth/signup. Tells support that a new user just signed up."""
    plan = "Beta (60-day trial)" if user.get("signup_source") == "beta" else "Standard (7-day trial)"
    name = user.get("name") or "—"
    email = user.get("email") or "—"
    created = (user.get("created_at") or "")[:19].replace("T", " ")
    body_html = f"""
    <html><body style="font-family:-apple-system,'IBM Plex Sans',sans-serif; color:#1C1917;">
      <div style="max-width:540px; line-height:1.6;">
        <p style="font-size:18px; font-weight:600; margin-bottom:8px;">New Steno Desk signup</p>
        <table style="border-collapse:collapse; font-size:14px;">
          <tr><td style="padding:4px 12px 4px 0; color:#78716C;">Name</td><td style="padding:4px 0;">{name}</td></tr>
          <tr><td style="padding:4px 12px 4px 0; color:#78716C;">Email</td><td style="padding:4px 0;"><a href="mailto:{email}">{email}</a></td></tr>
          <tr><td style="padding:4px 12px 4px 0; color:#78716C;">Plan</td><td style="padding:4px 0;">{plan}</td></tr>
          <tr><td style="padding:4px 12px 4px 0; color:#78716C;">Signed up</td><td style="padding:4px 0;">{created} UTC</td></tr>
        </table>
        <hr style="margin:20px 0; border:none; border-top:1px solid #E7E5E4;">
        <p style="font-size:12px; color:#78716C;">Sent automatically from Steno Desk. View all users in the admin panel.</p>
      </div>
    </body></html>
    """
    body_text = (
        f"New Steno Desk signup\n\n"
        f"Name: {name}\nEmail: {email}\nPlan: {plan}\nSigned up: {created} UTC\n\n"
        f"— Steno Desk admin notifier"
    )
    return send_admin_notification(
        subject=f"New signup: {name} ({email})",
        body_html=body_html,
        body_text=body_text,
    )


def send_new_lead_notification(email: str, source: str) -> Tuple[bool, Optional[str]]:
    """Fired from /api/leads. Tells support a new wait-list email was captured."""
    body_html = f"""
    <html><body style="font-family:-apple-system,'IBM Plex Sans',sans-serif; color:#1C1917;">
      <div style="max-width:540px; line-height:1.6;">
        <p style="font-size:18px; font-weight:600; margin-bottom:8px;">New Steno Desk lead</p>
        <p>Someone just submitted the landing-page email capture form.</p>
        <table style="border-collapse:collapse; font-size:14px;">
          <tr><td style="padding:4px 12px 4px 0; color:#78716C;">Email</td><td style="padding:4px 0;"><a href="mailto:{email}">{email}</a></td></tr>
          <tr><td style="padding:4px 12px 4px 0; color:#78716C;">Source</td><td style="padding:4px 0;">{source}</td></tr>
        </table>
        <hr style="margin:20px 0; border:none; border-top:1px solid #E7E5E4;">
        <p style="font-size:12px; color:#78716C;">Sent automatically from Steno Desk.</p>
      </div>
    </body></html>
    """
    body_text = (
        f"New Steno Desk lead\n\nEmail: {email}\nSource: {source}\n\n"
        f"— Steno Desk admin notifier"
    )
    return send_admin_notification(
        subject=f"New lead: {email}",
        body_html=body_html,
        body_text=body_text,
    )

