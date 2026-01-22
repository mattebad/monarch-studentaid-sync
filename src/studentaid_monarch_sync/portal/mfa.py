from __future__ import annotations

import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import Message
from email.utils import parsedate_to_datetime
import html as _html
from typing import Optional

from ..config import GmailImapConfig


logger = logging.getLogger(__name__)


def _safe_imap_logout(mail: Optional[imaplib.IMAP4_SSL]) -> None:
    if mail is None:
        return
    try:
        mail.close()
    except Exception:
        pass
    try:
        mail.logout()
    except Exception:
        pass


def _imap_connect_and_select(cfg: GmailImapConfig) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(cfg.user, cfg.app_password)
    sel_status, _ = mail.select(cfg.folder)
    if sel_status != "OK":
        raise RuntimeError(f"IMAP select failed for folder={cfg.folder!r}: {sel_status}")
    return mail


def poll_gmail_imap_for_code(
    cfg: GmailImapConfig,
    *,
    timeout_seconds: int = 120,
    poll_interval_seconds: int = 5,
    print_code: bool = False,
) -> str:
    """
    Poll Gmail IMAP for an unseen MFA email and extract a code using cfg.code_regex.

    Requires:
    - Gmail account has 2-step verification enabled
    - An App Password is generated
    - IMAP access is enabled on the account
    """
    deadline = time.time() + timeout_seconds
    code_re = re.compile(cfg.code_regex)

    # Only accept MFA emails received after we started polling (with a small tolerance),
    # so we don't accidentally reuse a stale/older code email still sitting in the label.
    started_at = datetime.now(timezone.utc)
    min_received_at = started_at - timedelta(seconds=30)

    # Small in-memory cache of already-checked messages during this polling window.
    # This avoids refetching/parsing the same emails every few seconds.
    checked_msg_ids: set[bytes] = set()

    preferred_res = [
        # In StudentAid-servicer HTML emails, the actual OTP is commonly inside a callout:
        #   <p class="h2 ...">437311</p>
        re.compile(r'<p[^>]*class="[^"]*\\bh2\\b[^"]*"[^>]*>\\s*(\\d{6})\\s*</p>', re.I),
        # Prefer codes near common phrases (email is usually "Authorization Code: 123456")
        re.compile(r"authorization\s+code[^0-9]{0,30}(\d{6})", re.I),
        re.compile(r"authentication\s+code[^0-9]{0,30}(\d{6})", re.I),
        re.compile(r"one[-\s]?time[^0-9]{0,30}(\d{6})", re.I),
        re.compile(r"six[-\s]?digit[^0-9]{0,50}(\d{6})", re.I),
    ]

    mail: Optional[imaplib.IMAP4_SSL] = None
    try:
        while time.time() < deadline:
            try:
                # Reuse a single IMAP session across polls to avoid repeated TLS handshakes/logins
                # (which can be slow and can trip Gmail security throttles).
                if mail is None:
                    mail = _imap_connect_and_select(cfg)
                else:
                    try:
                        mail.noop()
                    except Exception:
                        _safe_imap_logout(mail)
                        mail = _imap_connect_and_select(cfg)

                code = _try_fetch_code_once(
                    cfg,
                    mail=mail,
                    code_re=code_re,
                    preferred_res=preferred_res,
                    min_received_at=min_received_at,
                    print_code=print_code,
                    checked_msg_ids=checked_msg_ids,
                )
                if code:
                    return code
            except Exception:
                # Best-effort: reconnect on transient failures.
                logger.debug("IMAP poll attempt failed; reconnecting.", exc_info=True)
                _safe_imap_logout(mail)
                mail = None

            time.sleep(poll_interval_seconds)
    finally:
        _safe_imap_logout(mail)

    raise TimeoutError(f"Timed out waiting for MFA code email after {timeout_seconds}s")


def _try_fetch_code_once(
    cfg: GmailImapConfig,
    *,
    mail: imaplib.IMAP4_SSL,
    code_re: re.Pattern[str],
    preferred_res: list[re.Pattern[str]],
    min_received_at: datetime,
    print_code: bool,
    checked_msg_ids: set[bytes],
) -> Optional[str]:
    try:
        # Re-select in case the server dropped the selected mailbox between polls.
        sel_status, _ = mail.select(cfg.folder)
        if sel_status != "OK":
            raise RuntimeError(f"IMAP select failed for folder={cfg.folder!r}: {sel_status}")

        # Search ALL so we can still find the message even if Gmail/filter marks it read.
        search_parts: list[str] = ["ALL"]
        if cfg.sender_hint:
            search_parts += ["FROM", f"\"{cfg.sender_hint}\""]
        # If the user didn't configure a hint, default to the common subject shown on the MFA page.
        subject = (cfg.subject_hint or "").strip() or "Authorization Code"
        search_parts += ["SUBJECT", f"\"{subject}\""]

        status, data = mail.search(None, *search_parts)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status} {data}")

        ids = data[0].split()
        if not ids:
            return None
    except Exception:
        # Treat transient IMAP parsing/search issues as "no code yet"; caller will retry.
        logger.debug("IMAP fetch attempt failed; treating as no-code.", exc_info=True)
        return None

    # Newest first
    for msg_id in reversed(ids[-25:]):
        if msg_id in checked_msg_ids:
            continue
        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        checked_msg_ids.add(msg_id)

        raw = msg_data[0][1]
        msg = message_from_bytes(raw)
        received_at = _best_effort_msg_datetime_utc(msg)
        if not received_at:
            continue
        if received_at < min_received_at:
            # Too old; keep looking for a fresh code generated for *this* login.
            continue

        body = _extract_best_effort_body(msg)

        code = _extract_code(body, preferred_res=preferred_res, fallback_re=code_re)
        if not code:
            continue
        # Mark as seen so we don't reuse the same email.
        try:
            mail.store(msg_id, "+FLAGS", "\\Seen")
        except Exception:
            logger.debug("Failed to mark message as seen (msg_id=%s).", msg_id, exc_info=True)

        subject = (msg.get("Subject") or "").strip()
        sender = (msg.get("From") or "").strip()
        ts = received_at.isoformat()

        masked = f"{code[:2]}****{code[-2:]}" if len(code) >= 4 else "***"
        logger.info(
            "Fetched MFA code from email (received_at=%s subject=%r from=%r code=%s)",
            ts,
            subject,
            sender,
            masked,
        )
        # For headful debugging, optionally print the full code to the terminal so you can
        # visually confirm we're grabbing the correct email. This is intentionally not logged
        # to the file handler.
        if print_code:
            print(f"[MFA] code={code} received_at={ts} subject={subject!r}")
        return code

    return None


def _extract_best_effort_body(msg: Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    parts.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


def _best_effort_msg_datetime_utc(msg: Message) -> Optional[datetime]:
    raw_date = (msg.get("Date") or "").strip()
    if not raw_date:
        return None
    try:
        dt = parsedate_to_datetime(raw_date)
    except Exception:
        return None
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _extract_code(body: str, *, preferred_res: list[re.Pattern[str]], fallback_re: re.Pattern[str]) -> Optional[str]:
    # 1) Try preferred patterns on the raw body (works for HTML-specific patterns)
    for r in preferred_res:
        m = r.search(body)
        if m:
            return m.group(1)

    # 2) Strip HTML/CSS to avoid false matches like the CSS hex color "#265179"
    text = _strip_html_to_text(body)

    # Re-run preferred patterns against text (for phrase-based patterns)
    for r in preferred_res:
        m = r.search(text)
        if m:
            return m.group(1)

    # 3) Fallback: find any 6-digit match, but explicitly ignore hex colors or embedded numbers.
    for m in fallback_re.finditer(text):
        code = m.group(1)
        # Ignore if preceded by a '#' (CSS hex color) in the original body.
        start = m.start(1)
        if start > 0 and text[start - 1] == "#":
            continue
        return code

    return None


def _strip_html_to_text(s: str) -> str:
    # Remove style/script blocks and comments
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    # Remove tags
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


