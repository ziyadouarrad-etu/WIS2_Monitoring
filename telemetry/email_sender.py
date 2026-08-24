import base64
import logging
import os
from email.message import EmailMessage
from email.utils import formataddr

import requests

logger = logging.getLogger("WIS2_Email")

GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

GMAIL_REQUIRED_ENV = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")


def is_configured():
    return all(os.environ.get(key) for key in GMAIL_REQUIRED_ENV)


def _sender_email():
    return os.environ.get("GMAIL_SENDER_EMAIL") or os.environ.get("SMTP_USER")


def _access_token():
    resp = requests.post(
        GMAIL_TOKEN_URL,
        data={
            "client_id": os.environ["GMAIL_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(_api_error("token", resp))
    return resp.json()["access_token"]


def _build_raw_message(subject, body, to_email):
    msg = EmailMessage()
    msg["Subject"] = subject
    sender = _sender_email()
    if sender:
        msg["From"] = formataddr(("WIS2 Monitoring", sender))
    msg["To"] = to_email
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _api_error(step, resp):
    try:
        body = resp.json()
        err = body.get("error")
    except ValueError:
        body, err = {}, None
    if isinstance(err, dict):
        detail = err.get("message") or resp.text
    elif isinstance(err, str):
        # OAuth endpoints return {"error": "invalid_grant", "error_description": "..."}
        desc = body.get("error_description")
        detail = str(err) + (f" — {desc}" if desc else "")
    else:
        detail = resp.text
    return f"Gmail API error at {step} ({resp.status_code}): {detail}"


def _send_via_smtp(subject, body, to_email):
    from django.core.mail import send_mail
    send_mail(
        subject=subject,
        message=body,
        from_email=None,
        recipient_list=[to_email],
        fail_silently=False,
    )


def _send_via_gmail(subject, body, to_email):
    try:
        token = _access_token()
        raw = _build_raw_message(subject, body, to_email)
        resp = requests.post(
            GMAIL_SEND_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise RuntimeError(_api_error("send", resp))
    except requests.RequestException as exc:
        raise RuntimeError(f"Gmail API network error: {exc}") from exc


def send_email(subject, body, to_email):
    """Send via SMTP first, falling back to the Gmail API when SMTP fails.

    Raises a RuntimeError describing both failures when every path fails.
    """
    try:
        _send_via_smtp(subject, body, to_email)
        return
    except Exception as smtp_err:
        logger.warning("SMTP failed (%s); falling back to Gmail API", smtp_err)
        smtp_msg = str(smtp_err)

    if is_configured():
        try:
            _send_via_gmail(subject, body, to_email)
            return
        except Exception as api_err:
            raise RuntimeError(
                f"SMTP failed: {smtp_msg} — Gmail API failed: {api_err}"
            ) from api_err

    raise RuntimeError(
        f"SMTP failed: {smtp_msg} — Gmail API is not configured"
    ) from None
