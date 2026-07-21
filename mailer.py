#!/usr/bin/env python3
"""Shared Gmail SMTP sender for check.py's alerts and agent_email.py's
replies. Always sends to MAIL_TO -- the private address that never
appears as a command sender (see agent_email.py's gates)."""

import os
import smtplib
from email.mime.text import MIMEText


def send_email(subject, body):
    """Send a plain-text email to MAIL_TO. Raises smtplib.SMTPException on
    failure; callers already handle that the same as any other action
    failure (log scrubbed, skip)."""
    mail_user = os.environ["MAIL_USERNAME"]
    mail_pass = os.environ["MAIL_APP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_user
    msg["To"] = mail_to

    recipients = [addr.strip() for addr in mail_to.split(",") if addr.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(mail_user, mail_pass)
        server.sendmail(mail_user, recipients, msg.as_string())
