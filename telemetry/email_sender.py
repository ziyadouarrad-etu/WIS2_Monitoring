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
        detail = resp.json().get("error", {}).get("message") or resp.text
    except ValueError:
        detail = resp.text
    return f"Gmail API error at {step} ({resp.status_code}): {detail}"


def send_email(subject, body, to_email):
    if not is_configured():
        from django.core.mail import send_mail
        send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[to_email],
            fail_silently=False,
        )
        return
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
