"""Beta-trial cutoff regression tests (2026-10-08).

The 60-day beta trial was open-ended via ?beta=1 with no expiry. The
offer now closes at routers.auth.BETA_TRIAL_CUTOFF (2026-10-22 EOD UTC):
after that instant, beta-flagged signups get the standard 7-day trial.

Two tests cover both sides of the cutoff. One of them requires faking
the clock — we monkeypatch routers.auth.db.users.find_one/insert_one in
an in-process call so no DB dependency is needed. Requires a reachable
server ONLY for the pre-cutoff leg (it hits the real HTTP endpoint).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

BASE = os.environ.get("TEST_BASE_URL", "http://localhost:8001")
API = f"{BASE}/api"


def _server_beta_open() -> bool:
    """Whether the server under test was booted before the cutoff.

    The cutoff lives on the server process; this test process can't read
    its clock. We approximate: if the test runs before the real cutoff,
    the server (booted today) is also pre-cutoff. After the real cutoff
    the pre-cutoff assertion below would be wrong, so the leg self-
    retires — exactly the point of a dated offer.
    """
    cutoff = datetime(2026, 10, 22, 23, 59, 59, tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < cutoff


def test_beta_signup_before_cutoff_gets_60_days():
    if not _server_beta_open():
        pytest.skip("cutoff passed — pre-cutoff offer no longer active in real time")
    email = f"beta_pre_{uuid.uuid4().hex[:6]}@test.com"
    r = httpx.post(f"{API}/auth/signup",
                   json={"email": email, "password": "depo1234", "name": "Beta", "beta": True},
                   timeout=20.0)
    assert r.status_code == 200, r.text
    u = r.json()["user"]
    assert u["signup_source"] == "beta"
    assert u["trial_days_granted"] == 60


def test_beta_signup_after_cutoff_gets_7_days(monkeypatch):
    """Post-cutoff beta flag must fall through to the standard 7-day trial.

    Simulates the server having booted after the cutoff by patching the
    in-process auth router's clock source; runs the signup handler
    directly (no HTTP), with a throwaway in-memory collection stub.
    """
    from datetime import datetime as _dt

    import routers.auth as auth_mod

    class _FakeNow:
        @staticmethod
        def now(tz=None):
            return auth_mod.BETA_TRIAL_CUTOFF + timedelta(days=1)

    class _FakeCol:
        async def find_one(self, q):
            return None

        async def insert_one(self, doc):
            _FakeCol.last = doc

    class _FakeDB:
        users = _FakeCol()

    # The handler computes `now = datetime.now(timezone.utc)` — patch the
    # datetime symbol inside the module namespace.
    real_datetime = auth_mod.datetime

    class _DT(_dt):
        @classmethod
        def now(cls, tz=None):
            return auth_mod.BETA_TRIAL_CUTOFF + timedelta(days=1)

    monkeypatch.setattr(auth_mod, "datetime", _DT, raising=True)
    monkeypatch.setattr(auth_mod, "db", _FakeDB, raising=True)
    monkeypatch.setattr(auth_mod, "send_new_signup_notification",
                        lambda doc: None, raising=True)

    # Build a fake Request with beta=1 in query params
    class _Q:
        def get(self, k):
            return "1" if k == "beta" else None

    class _Req:
        query_params = _Q()
        headers = {}
        client = None

    payload = auth_mod.SignupIn(email="post_cutoff@test.com",
                                password="depo1234", name="PC", beta=True)
    # Signature: (payload, request, response, background)
    import asyncio

    class _Resp:
        def set_cookie(self, *a, **k):
            pass

    class _BG:
        def add_task(self, *a, **k):
            pass

    result = asyncio.run(auth_mod.signup(payload, _Req(), _Resp(), _BG()))
    assert result["user"]["trial_days_granted"] == 7, (
        "post-cutoff beta signup must NOT get 60 days")
    assert result["user"]["signup_source"] == "direct"
    assert _FakeCol.last["email"] == "post_cutoff@test.com"