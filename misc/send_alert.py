import smtplib
from email.message import EmailMessage
import os
import sys
from misc.load_parameters import parameters
from misc.log_test import logger

LOG_D = {'module_name': 'send_alert'}

def send_alert_email(timestamp, hazard_details):
    try:
        brevo_params = parameters.get_parameters().get('brevo', {})
        
        BREVO_SMTP_HOST = brevo_params.get('BREVO_SMTP_HOST')
        BREVO_SMTP_PORT = brevo_params.get('BREVO_SMTP_PORT')
        BREVO_SMTP_USER = brevo_params.get('BREVO_SMTP_USER')
        BREVO_SMTP_KEY = brevo_params.get('BREVO_SMTP_KEY')
        FROM_EMAIL = brevo_params.get('FROM_EMAIL')
        ALERT_TO = brevo_params.get('ALERT_TO')

        if not all([BREVO_SMTP_HOST, BREVO_SMTP_PORT, BREVO_SMTP_USER, BREVO_SMTP_KEY, FROM_EMAIL, ALERT_TO]):
            logger.warning("Brevo email settings are incomplete. Skipping email alert.", extra=LOG_D)
            return

        subject = f"HAZARD ALERT - Audio Surveillance System"
        body = f"""
HAZARD DETECTED

Time: {timestamp}

Assessment:
{hazard_details}

Please check the dashboard immediately.
        """

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL
        msg["To"] = ALERT_TO
        msg.set_content(body)

        smtp = smtplib.SMTP(BREVO_SMTP_HOST, BREVO_SMTP_PORT, timeout=10)
        smtp.starttls()
        smtp.login(BREVO_SMTP_USER.strip(), BREVO_SMTP_KEY.strip())
        smtp.send_message(msg)
        smtp.quit()
        
        logger.info(f"Alert email sent to {ALERT_TO}", extra=LOG_D)

    except Exception as e:
        logger.error(f"Failed to send alert email: {e}", extra=LOG_D)
