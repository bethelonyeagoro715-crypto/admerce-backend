from __future__ import print_function
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

configuration = sib_api_v3_sdk.Configuration()
import os
configuration.api_key['api-key'] = os.getenv("SENDINBLUE_API_KEY")

FROM_EMAIL = "admerceinc@gmail.com"

def send_otp_email(recipient_email: str, otp: str, purpose: str = "reset_password"):
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

    subject = "Your Admerce Verification Code"
    body = f"""
Hello,

Your verification code is: {otp}

This code is valid for 10 minutes. Use it to {purpose.replace('_', ' ')}.

If you didn't request this, please ignore this email.

— The Admerce Team
    """

    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": recipient_email}],
        sender={"email": FROM_EMAIL, "name": "Admerce"},
        subject=subject,
        text_content=body
    )

    try:
        api_instance.send_transac_email(send_smtp_email)
        print(f"✅ OTP email sent to {recipient_email}")
        return True
    except ApiException as e:
        print(f"❌ Failed to send email: {e}")
        return False