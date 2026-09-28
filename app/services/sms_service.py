"""
OTP delivery over SMS. Provider selected at runtime via env var
SMS_PROVIDER. All sends are async and non-blocking.

Supported providers (all optional — set SMS_PROVIDER to one of):
  - twilio   : global, ~$0.0075/SMS, most reliable international coverage
  - termii   : Nigeria/Africa focused, cheap for NG
  - vonage   : global, previously Nexmo

When SMS_PROVIDER is unset or "disabled", send_otp_sms() is a no-op
and returns False. Email remains the sole channel in that case.
"""
import os
import logging
import httpx

logger = logging.getLogger("sms")

SMS_PROVIDER = (os.getenv("SMS_PROVIDER") or "disabled").strip().lower()

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")  # E.164, e.g. +12025550123

# Termii
TERMII_API_KEY = os.getenv("TERMII_API_KEY")
TERMII_SENDER_ID = os.getenv("TERMII_SENDER_ID", "Admerce")

# Vonage
VONAGE_API_KEY = os.getenv("VONAGE_API_KEY")
VONAGE_API_SECRET = os.getenv("VONAGE_API_SECRET")
VONAGE_FROM = os.getenv("VONAGE_FROM", "Admerce")

# Message templates per purpose. Keep them short — some carriers bill
# per 160 chars, and long messages get flagged as promotional.
_TEMPLATES = {
    "signup_verify": "Admerce code: {code}. Expires in 10 min. Never share this code.",
    "reset_password": "Admerce password reset code: {code}. Expires in 10 min.",
}

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def is_sms_configured() -> bool:
    """True only when the selected provider has every required value."""
    if SMS_PROVIDER == "twilio":
        return all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER])
    if SMS_PROVIDER == "termii":
        return bool(TERMII_API_KEY)
    if SMS_PROVIDER == "vonage":
        return all([VONAGE_API_KEY, VONAGE_API_SECRET])
    return False


def _body(code: str, purpose: str) -> str:
    tpl = _TEMPLATES.get(purpose, "Admerce code: {code}.")
    return tpl.format(code=code)


async def send_otp_sms(to_e164: str, code: str, purpose: str) -> bool:
    """
    Fire an OTP SMS. Returns True on success, False otherwise.
    Never raises — a failed SMS must not block account creation.
    """
    if not is_sms_configured():
        logger.info("SMS not configured — skipping send to %s", to_e164)
        return False
    if not to_e164 or not to_e164.startswith("+"):
        logger.warning("SMS skipped — bad E.164 target: %r", to_e164)
        return False

    body = _body(code, purpose)
    try:
        if SMS_PROVIDER == "twilio":
            return await _send_twilio(to_e164, body)
        if SMS_PROVIDER == "termii":
            return await _send_termii(to_e164, body)
        if SMS_PROVIDER == "vonage":
            return await _send_vonage(to_e164, body)
    except Exception as e:
        logger.error("send_otp_sms(%s) failed: %r", SMS_PROVIDER, e)
        return False
    logger.warning("SMS_PROVIDER=%r has no handler", SMS_PROVIDER)
    return False


# ── Provider implementations ─────────────────────────────────────────
async def _send_twilio(to: str, body: str) -> bool:
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/"
        f"{TWILIO_ACCOUNT_SID}/Messages.json"
    )
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            data={"From": TWILIO_FROM_NUMBER, "To": to, "Body": body},
        )
    if r.status_code >= 400:
        logger.error("Twilio %s: %s", r.status_code, r.text[:300])
        return False
    return True


async def _send_termii(to: str, body: str) -> bool:
    url = "https://api.ng.termii.com/api/sms/send"
    payload = {
        "to": to,
        "from": TERMII_SENDER_ID,
        "sms": body,
        "type": "plain",
        "channel": "generic",
        "api_key": TERMII_API_KEY,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(url, json=payload)
    if r.status_code >= 400:
        logger.error("Termii %s: %s", r.status_code, r.text[:300])
        return False
    return True


async def _send_vonage(to: str, body: str) -> bool:
    url = "https://rest.nexmo.com/sms/json"
    payload = {
        "api_key": VONAGE_API_KEY,
        "api_secret": VONAGE_API_SECRET,
        "from": VONAGE_FROM,
        "to": to,
        "text": body,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(url, data=payload)
    if r.status_code >= 400:
        logger.error("Vonage %s: %s", r.status_code, r.text[:300])
        return False
    # Vonage returns 200 even on logical failures — check the body.
    try:
        data = r.json()
        status = data.get("messages", [{}])[0].get("status")
        if status not in (None, "0"):
            logger.error("Vonage logical error: %s", data)
            return False
    except Exception:
        pass
    return True