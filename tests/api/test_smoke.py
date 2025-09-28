import os
import httpx
import pytest

GITEA_BASE = os.getenv("GITEA_BASE", "http://localhost:3000/api/v1").rstrip("/")
REVIEWER_BASE = os.getenv("REVIEWER_BASE", "http://localhost:8082").rstrip("/")

@pytest.mark.asyncio
async def test_reviewer_health():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{REVIEWER_BASE}/health")
        r.raise_for_status()
        assert r.json().get("status") == "ok"

@pytest.mark.asyncio
async def test_gitea_version():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{GITEA_BASE}/version")
        r.raise_for_status()
        assert "version" in r.json()
