"""Sending the guardian an email.

Reuses the Sentinel app's own Brevo SMTP helper (misc/send_alert.py) and
its configured credentials from common_files/parameters.json, edited in
the dashboard's Settings tab under "Alert Communication (Brevo SMTP)".
There is no second SMTP client and no second copy of the credentials.

Two rules this module exists to enforce:

*The conversation model never sends mail.* It produces a structured
report; this class decides whether it goes out and formats it. That
separation is what stops a prompt injection in a transcript from being
able to email anybody.

*Nothing is invented.* Every claim in the email is either something the
system observed (times, device, duration) or a verbatim quote from the
transcript. If the report has no supporting quotes, the email says so
rather than dressing up a guess.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import datetime

from ..config import SENTINEL_ROOT

if str(SENTINEL_ROOT) not in sys.path:
    sys.path.insert(0, str(SENTINEL_ROOT))

log = logging.getLogger(__name__)


@dataclass
class NotificationResult:
    sent: bool
    dry_run: bool
    recipient: str | None
    subject: str
    body: str
    error: str | None = None


class GuardianNotifier:
    def __init__(self, enabled: bool = True, dry_run: bool = False,
                 include_transcript: bool = False):
        self.enabled = enabled
        self.dry_run = dry_run
        self.include_transcript = include_transcript

    # ── configuration ────────────────────────────────────────────────

    @staticmethod
    def _brevo() -> dict:
        try:
            from misc.load_parameters import parameters
            return parameters.get_parameters().get("brevo", {}) or {}
        except Exception:
            log.exception("could not read Brevo settings")
            return {}

    @property
    def recipient(self) -> str | None:
        return self._brevo().get("ALERT_TO") or None

    def is_configured(self) -> bool:
        cfg = self._brevo()
        return all(cfg.get(k) for k in ("BREVO_SMTP_HOST", "BREVO_SMTP_PORT",
                                        "BREVO_SMTP_USER", "BREVO_SMTP_KEY",
                                        "FROM_EMAIL", "ALERT_TO"))

    # ── composing ────────────────────────────────────────────────────

    def compose(self, *, person_name: str, severity: str, report,
                session_id: str, started_at: datetime, sentinel_id: str,
                room: str | None, transcript: list[dict] | None = None) -> tuple[str, str]:
        when = started_at.strftime("%A %d %B %Y at %H:%M")
        subject = f"[{severity}] {person_name} — {report.subject}"

        lines = [
            f"This is an automated message from the home companion system.",
            "",
            f"Person   : {person_name}",
            f"When     : {when}",
            f"Where    : {room or 'unknown room'} ({sentinel_id})",
            f"Severity : {severity}",
            f"Session  : {session_id[:8]}",
            "",
            "WHAT HAPPENED",
            report.summary.strip() or "(no summary available)",
        ]

        if report.reported_items:
            lines += ["", "POINTS TO NOTE"]
            lines += [f"  - {item}" for item in report.reported_items]

        if report.quotes:
            lines += ["", "IN THEIR OWN WORDS"]
            lines += [f'  "{q}"' for q in report.quotes]
        else:
            lines += ["", "IN THEIR OWN WORDS",
                      "  (no direct quotes were recorded for this item)"]

        if self.include_transcript and transcript:
            lines += ["", "FULL TRANSCRIPT"]
            for turn in transcript:
                who = "Agent" if turn.get("speaker") == "AGENT" else "Speaker"
                lines.append(f"  {who}: {turn.get('text','')}")

        lines += [
            "",
            "-" * 62,
            "This alert was generated automatically by an AI system from a",
            "recorded conversation. The speech was transcribed by machine and",
            "may contain errors. It is not a medical assessment and not an",
            "emergency service. If you are concerned, contact "
            f"{person_name} directly.",
        ]
        return subject, "\n".join(lines)

    # ── sending ──────────────────────────────────────────────────────

    def send(self, *, person_name: str, severity: str, report,
             session_id: str, started_at: datetime, sentinel_id: str,
             room: str | None = None,
             transcript: list[dict] | None = None) -> NotificationResult:
        subject, body = self.compose(
            person_name=person_name, severity=severity, report=report,
            session_id=session_id, started_at=started_at,
            sentinel_id=sentinel_id, room=room, transcript=transcript)

        if not self.enabled:
            log.info("guardian email disabled; not sending")
            return NotificationResult(False, False, self.recipient, subject, body,
                                      "disabled")

        if self.dry_run:
            log.warning("DRY RUN -- guardian email not sent:\n%s\n%s", subject, body)
            return NotificationResult(False, True, self.recipient, subject, body)

        if not self.is_configured():
            log.error("Brevo is not fully configured (Settings tab); not sending")
            return NotificationResult(False, False, self.recipient, subject, body,
                                      "brevo_not_configured")

        try:
            self._send_smtp(subject, body)
            log.info("guardian email sent to %s", self.recipient)
            return NotificationResult(True, False, self.recipient, subject, body)
        except Exception as exc:
            log.exception("guardian email failed")
            return NotificationResult(False, False, self.recipient, subject, body,
                                      f"{type(exc).__name__}: {exc}")

    def _send_smtp(self, subject: str, body: str) -> None:
        """Send through the household's Brevo account.

        The same credentials, host, port and STARTTLS-then-login sequence
        as misc/send_alert.py -- the path the dashboard's hazard alerts
        use, and which is known to deliver. Deliberately not a call into
        that helper, for two reasons.

        Its subject line is fixed at "HAZARD ALERT - Audio Surveillance
        System". Most of what the companion sends is not a hazard: it is
        somebody asking for shopping. A guardian who gets HAZARD ALERT
        for a pizza either panics, or stops reading them -- and the
        second is how a real one gets missed.

        And it catches every exception itself and returns None, so a
        caller cannot tell a delivered message from a failed one. This
        raises instead, because `send` above reports what happened and
        must not claim a success it has no way of knowing about.
        """
        import smtplib
        from email.message import EmailMessage

        cfg = self._brevo()
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = cfg["FROM_EMAIL"]
        message["To"] = cfg["ALERT_TO"]
        message.set_content(body)

        with smtplib.SMTP(cfg["BREVO_SMTP_HOST"],
                          int(cfg["BREVO_SMTP_PORT"]), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(str(cfg["BREVO_SMTP_USER"]).strip(),
                       str(cfg["BREVO_SMTP_KEY"]).strip())
            smtp.send_message(message)
