"""Shared pytest fixtures — boots a fresh test user against the live local
backend so smoke tests exercise the real wiring (Mongo, JWT, routers)."""
import os
import uuid
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

# Load backend/.env so tests that talk directly to Mongo (e.g. webhooks
# helpers) can resolve MONGO_URL / DB_NAME.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8001")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def api_url() -> str:
    return API


@pytest.fixture(scope="session")
def auth():
    """Sign up a fresh user once per test session and return Bearer headers."""
    email = f"pytest-{uuid.uuid4().hex[:10]}@example.com"
    password = "depo1234!"
    with httpx.Client(base_url=API, timeout=20.0) as c:
        r = c.post("/auth/signup", json={"email": email, "password": password, "name": "Pytest User"})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
    return {"email": email, "password": password, "token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture()
def client(auth):
    with httpx.Client(base_url=API, headers=auth["headers"], timeout=20.0) as c:
        yield c
