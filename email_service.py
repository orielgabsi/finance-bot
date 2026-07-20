import os

import resend


def send_email(to_email: str, subject: str, html_content: str) -> str:
    resend.api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ["RESEND_FROM_EMAIL"]

    result = resend.Emails.send({
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_content,
    })
    return result["id"]
