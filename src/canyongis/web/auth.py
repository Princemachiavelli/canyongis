"""Password + cookie session auth for the canyongis web server.

Sessions are signed (not encrypted) cookies carrying ``{"user_id": ...}``.
The signing secret is read from ``CANYONGIS_SESSION_SECRET`` or persisted to
``{output_root}/.session-secret`` on first run so cookies survive restarts.

If ``CANYONGIS_SINGLE_USER=1`` the ``current_user`` dependency auto-resolves
to the ``local`` user without inspecting cookies — keeping single-user dev
workflows ergonomic.
"""

from __future__ import annotations

import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import bcrypt
import psycopg
from fastapi import HTTPException, Response
from itsdangerous import BadSignature, URLSafeSerializer

from .db import PostgresConfig

SESSION_COOKIE_NAME = "canyongis_session"
SESSION_SALT = "canyongis.session.v1"
LOCAL_USERNAME = "local"


@dataclass(frozen=True)
class User:
    id: str
    username: str


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def _load_or_create_secret(output_root: Path) -> str:
    import contextlib

    env_secret = os.environ.get("CANYONGIS_SESSION_SECRET")
    if env_secret:
        return env_secret
    output_root.mkdir(parents=True, exist_ok=True)
    secret_path = output_root / ".session-secret"
    if secret_path.exists():
        return secret_path.read_text().strip()
    secret = secrets.token_urlsafe(48)
    secret_path.write_text(secret)
    with contextlib.suppress(OSError):
        os.chmod(secret_path, 0o600)
    return secret


def _is_single_user_mode() -> bool:
    return os.environ.get("CANYONGIS_SINGLE_USER", "").strip() in {"1", "true", "yes", "on"}


class AuthManager:
    """Encapsulates session signing and user lookup against the database."""

    def __init__(self, pg: PostgresConfig, *, secret: str, single_user: bool) -> None:
        self.pg = pg
        self.single_user = single_user
        self.serializer = URLSafeSerializer(secret, salt=SESSION_SALT)

    def _connect(self) -> psycopg.Connection:
        conn = psycopg.connect(**self.pg.connect_kwargs())
        conn.autocommit = True
        return conn

    def get_user_by_id(self, user_id: str) -> User | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, username FROM canyongis_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return User(id=row[0], username=row[1])

    def get_user_by_username(self, username: str) -> tuple[User, str] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash FROM canyongis_users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return User(id=row[0], username=row[1]), row[2]

    def authenticate(self, username: str, password: str) -> User | None:
        record = self.get_user_by_username(username)
        if record is None:
            return None
        user, password_hash = record
        if not verify_password(password, password_hash):
            return None
        return user

    def create_user(self, username: str, password: str) -> User:
        user_id = uuid.uuid4().hex[:16]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO canyongis_users (id, username, password_hash) VALUES (%s, %s, %s)",
                (user_id, username, hash_password(password)),
            )
        return User(id=user_id, username=username)

    def sign_session(self, user_id: str) -> str:
        return self.serializer.dumps({"user_id": user_id})

    def load_session(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            payload: Any = self.serializer.loads(token)
        except BadSignature:
            return None
        if not isinstance(payload, dict):
            return None
        user_id = payload.get("user_id")
        return user_id if isinstance(user_id, str) else None

    def local_user(self) -> User | None:
        record = self.get_user_by_username(LOCAL_USERNAME)
        return record[0] if record else None


def build_auth_manager(pg: PostgresConfig, output_root: Path) -> AuthManager:
    return AuthManager(
        pg,
        secret=_load_or_create_secret(output_root),
        single_user=_is_single_user_mode(),
    )


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=False,  # local dev; flip to True behind TLS in production
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def get_current_user(request, auth: AuthManager) -> User:
    """Resolve the active user from cookie session, or auto-pick 'local' in single-user mode."""
    if auth.single_user:
        user = auth.local_user()
        if user is None:
            raise HTTPException(
                status_code=500,
                detail="single-user mode is on but the 'local' user is missing",
            )
        return user
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user_id = auth.load_session(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = auth.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="session refers to unknown user")
    return user
