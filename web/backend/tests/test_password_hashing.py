"""Password hashing without passlib.

passlib has had no release since 2020 and pinned the backend to bcrypt 3.x.
Every stored hash — ``ADMIN_PASSWORD_HASH`` / ``_B64`` in the deploy env and
each row in ``users`` — was produced by passlib, so the replacement must
verify those byte-for-byte. The fixtures below are real passlib 1.7.4
output, captured before passlib was removed.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app import auth

# passlib.hash.bcrypt.hash("correct horse battery staple") — the default
# $2b$ ident at 12 rounds, which is what DEPLOY.md's recipe produced.
PASSLIB_2B = "$2b$12$ZweU6gkpZzpPPDq/NkwQ.OCPLPKTq9h58S8iulaozsO4XUWvJKyWu"
PASSLIB_2B_PASSWORD = "correct horse battery staple"
# The same hash as it sits in ADMIN_PASSWORD_HASH_B64 on Coolify.
PASSLIB_2B_B64 = "JDJiJDEyJFp3ZVU2Z2twWnpwUFBEcS9Oa3dRLk9DUExQS1RxOWg1OFM4aXVsYW96c080WFVXdkpLeVd1"
# An older $2a$ hash (passlib ident="2a"), in case any account predates $2b$.
PASSLIB_2A = "$2a$12$fO6Dw/xgr0GEBAuJiq1ZZuFgI0RUal6yMQvW6IVYm6CLzAvJRegD6"
PASSLIB_2A_PASSWORD = "legacy-2a-password"


def test_fixtures_are_what_they_claim():
    assert base64.b64decode(PASSLIB_2B_B64).decode() == PASSLIB_2B
    assert PASSLIB_2B.startswith("$2b$12$") and len(PASSLIB_2B) == 60
    assert PASSLIB_2A.startswith("$2a$12$") and len(PASSLIB_2A) == 60


def test_auth_does_not_need_passlib():
    """With passlib unimportable, auth still imports, hashes and verifies.

    Runs in a fresh interpreter: reloading app.auth in this process would
    replace get_current_user, and every later test's dependency override
    would then miss the function the routers hold.
    """
    script = (
        "import sys\n"
        "sys.modules['passlib'] = None\n"
        "sys.modules['passlib.hash'] = None\n"
        "from app import auth\n"
        f"assert auth.verify_password({PASSLIB_2B_PASSWORD!r}, {PASSLIB_2B!r})\n"
        "assert auth.verify_password('x', auth.hash_password('x'))\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize(
    ("password", "hashed"),
    [(PASSLIB_2B_PASSWORD, PASSLIB_2B), (PASSLIB_2A_PASSWORD, PASSLIB_2A)],
    ids=["2b", "2a"],
)
def test_existing_passlib_hashes_still_verify(password, hashed):
    assert auth.verify_password(password, hashed) is True
    assert auth.verify_password(password + "!", hashed) is False


def test_new_hashes_are_standard_bcrypt():
    hashed = auth.hash_password("s3cret")
    assert hashed.startswith("$2b$12$") and len(hashed) == 60
    assert auth.verify_password("s3cret", hashed) is True
    assert auth.verify_password("S3cret", hashed) is False


@pytest.mark.parametrize(
    ("password", "hashed"),
    [
        ("", PASSLIB_2B),
        (PASSLIB_2B_PASSWORD, ""),
        (PASSLIB_2B_PASSWORD, "not-a-bcrypt-hash"),
        (PASSLIB_2B_PASSWORD, PASSLIB_2B[:-1]),
        ("x" * 73, PASSLIB_2B),  # over bcrypt's 72-byte window
    ],
    ids=["empty-password", "empty-hash", "garbage-hash", "truncated-hash", "73-bytes"],
)
def test_bad_input_is_a_failed_check_not_an_exception(password, hashed):
    assert auth.verify_password(password, hashed) is False


def test_passwords_past_72_bytes_are_refused_not_truncated():
    """bcrypt reads only the first 72 bytes. Truncating (passlib's default)
    let a longer string authenticate as the 72-byte password it starts with;
    refusing keeps one password per account on every bcrypt version."""
    hashed = auth.hash_password("x" * 72)
    assert auth.verify_password("x" * 72, hashed) is True
    assert auth.verify_password("x" * 73, hashed) is False
    # 24 three-byte characters are 72 bytes; one more is 75.
    assert auth.verify_password("€" * 25, auth.hash_password("€" * 24)) is False
    with pytest.raises(ValueError, match="72 bytes"):
        auth.hash_password("x" * 73)
