"""FastAPI dependency injection for DB sessions, authenticated users, CSRF protection, and operator guards."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import User, UserSession
from app.db.session import get_db
from app.services.auth import SESSION_COOKIE_NAME, get_session_and_user


def _extract_raw_token(request: Request) -> str | None:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.lower().startswith("bearer "):
            raw_token = auth_hdr[7:].strip()
    return raw_token


def _enforce_cookie_csrf(request: Request, sess: UserSession) -> None:
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.cookies.get(SESSION_COOKIE_NAME):
        settings = get_settings()
        origin = request.headers.get("Origin")
        if origin and origin not in settings.cors_origins and settings.environment == "production":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "INVALID_ORIGIN", "message": "Cross-origin request blocked"},
            )
        csrf_hdr = request.headers.get("X-CSRF-Token", "")
        if csrf_hdr != sess.csrf_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "INVALID_CSRF", "message": "Missing or invalid CSRF token"},
            )


def get_current_auth(
    request: Request,
    db: Session = Depends(get_db),
) -> tuple[UserSession, User]:
    res = get_session_and_user(db, _extract_raw_token(request))
    if not res:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHENTICATED", "message": "Authentication required"},
        )
    sess, user = res
    _enforce_cookie_csrf(request, sess)
    return sess, user


def get_optional_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User | None:
    res = get_session_and_user(db, _extract_raw_token(request))
    if not res:
        return None
    sess, user = res
    _enforce_cookie_csrf(request, sess)
    return user


def get_current_user(auth: tuple[UserSession, User] = Depends(get_current_auth)) -> User:
    return auth[1]


def get_operator_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_operator:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "OPERATOR_REQUIRED", "message": "Operator permissions required"},
        )
    return user
