import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from string import Template

# ponytail: env only, no Settings class
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

DEFAULT_TEMPLATE = {
    "name": "default",
    "subject": "Reminder: pending documents for ${client_name}",
    "body": """Dear ${client_name},

This is a friendly reminder that we are waiting for the following documents:
- Sales/Purchase invoices
- Bank statements
- GST records

Please upload them at the earliest.

Thanks,
Accounts Team
""",
}


def _template_for(name: str | None) -> dict:
    # ponytail: only default template
    return DEFAULT_TEMPLATE


def render_reminder(client_name: str, template_name: str | None = None) -> tuple[str, str]:
    tpl = _template_for(template_name)
    subject = Template(tpl["subject"]).substitute(client_name=client_name)
    body = Template(tpl["body"]).substitute(client_name=client_name)
    return subject, body


def send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP not configured")
    if not to:
        raise ValueError("No recipient email")

    msg = MIMEMultipart()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


def send_reminder(client_email: str, client_name: str, template_name: str | None = None) -> tuple[str, str]:
    subject, body = render_reminder(client_name, template_name)
    send_email(client_email, subject, body)
    return subject, body
