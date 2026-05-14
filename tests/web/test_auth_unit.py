"""Unit tests for password hashing + cookie session signing — no database."""

from __future__ import annotations

from itsdangerous import URLSafeSerializer

from canyongis.web.auth import SESSION_SALT, hash_password, verify_password


def test_hash_round_trip() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)


def test_hash_is_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_session_signing_round_trip() -> None:
    s = URLSafeSerializer("secret-A", salt=SESSION_SALT)
    token = s.dumps({"user_id": "u1"})
    assert s.loads(token) == {"user_id": "u1"}


def test_session_signing_rejects_other_secret() -> None:
    import pytest
    from itsdangerous import BadSignature

    a = URLSafeSerializer("secret-A", salt=SESSION_SALT)
    b = URLSafeSerializer("secret-B", salt=SESSION_SALT)
    token = a.dumps({"user_id": "u1"})
    with pytest.raises(BadSignature):
        b.loads(token)
