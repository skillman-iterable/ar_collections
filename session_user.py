"""Resolve session user from IAP headers, env vars, or demo fallback."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from starlette.requests import Request


@dataclass
class SessionUser:
    email: str
    name: str


def _strip_iap_email(raw: str) -> str:
    """Strip IAP prefix: 'accounts.google.com:user@co.com' -> 'user@co.com'"""
    if ":" in raw and "@" in raw:
        return raw.split(":", 1)[-1].strip()
    return raw.strip()


def _name_from_email(email: str) -> str:
    if not email or "@" not in email:
        return ""
    local = email.split("@", 1)[0]
    parts = re.split(r"[._+\-]+", local)
    return " ".join(p.capitalize() for p in parts if p)


def session_user(request: Request | None = None) -> SessionUser:
    """Resolve the logged-in user.

    Priority:
      1. IAP headers (production behind Google Cloud IAP)
      2. Environment variables (local dev: AR_SESSION_EMAIL / AR_SESSION_NAME)
      3. Demo identity (only when AR_USE_DEMO=1)
    """
    email = ""
    name = ""

    if request is not None:
        h = request.headers
        email = _strip_iap_email(
            h.get("x-goog-authenticated-user-email")
            or h.get("x-forwarded-email")
            or ""
        )
        name = (
            h.get("x-goog-authenticated-user-name")
            or h.get("x-forwarded-user")
            or ""
        ).strip()

    if not email:
        email = os.getenv("AR_SESSION_EMAIL", "")
    if not name:
        name = os.getenv("AR_SESSION_NAME", "")

    if not email and os.getenv("AR_USE_DEMO", "").lower() in ("1", "true", "yes"):
        email = "shawn.skillman@iterable.com"
    if not name and email:
        name = _name_from_email(email)
    if not name and os.getenv("AR_USE_DEMO", "").lower() in ("1", "true", "yes"):
        name = "Shawn Skillman"

    return SessionUser(email=email, name=name)
