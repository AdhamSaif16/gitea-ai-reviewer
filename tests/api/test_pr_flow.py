import os, base64, time, json
import httpx
import pytest
import anyio

# ----- config (env-driven to stay IP-proof on EC2) -----
GITEA_BASE = os.getenv("GITEA_BASE", "http://localhost:3000/api/v1").rstrip("/")
OWNER = os.getenv("OWNER", "gitea")
REPO = os.getenv("REPO", "ai-review-demo")

def _load_token() -> str:
    tok = os.getenv("GITEA_TOKEN")
    if tok:
        return tok.strip()
    # fallbacks for our EC2 setup
    for p in ("/run/secrets/gitea_token", os.path.expanduser("~/stack/secrets/gitea_token")):
        if os.path.exists(p):
            return open(p, "r", encoding="utf-8").read().strip()
    pytest.skip("GITEA_TOKEN missing (set env or mount secret)")

TOKEN = _load_token()
HDRS = {"Authorization": f"token {TOKEN}"}

# ----- helpers -----
async def _get(client: httpx.AsyncClient, path: str, **params):
    r = await client.get(f"{GITEA_BASE}{path}", headers=HDRS, params=params)
    r.raise_for_status()
    return r.json()

async def _post(client: httpx.AsyncClient, path: str, payload: dict):
    r = await client.post(f"{GITEA_BASE}{path}", headers=HDRS, json=payload)
    r.raise_for_status()
    return r.json()

async def _put(client: httpx.AsyncClient, path: str, payload: dict):
    r = await client.put(f"{GITEA_BASE}{path}", headers=HDRS, json=payload)
    r.raise_for_status()
    return r.json()

def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

@pytest.mark.anyio
async def test_ai_reviewer_end_to_end():
    async with httpx.AsyncClient(timeout=30) as c:
        # 1) discover default branch
        repo = await _get(c, f"/repos/{OWNER}/{REPO}")
        base_branch = repo.get("default_branch") or "main"

        # 2) create a unique feature branch via "contents" API (new_branch)
        ts = int(time.time())
        branch = f"e2e-ai-{ts}"
        path = "app/vuln_demo.py"
        code = (
            "import subprocess,re,httpx\n"
            "_evil = re.compile(r'(a+)+$')\n"
            "def run(cmd):\n"
            "    return subprocess.check_output(cmd, shell=True)  # noqa: S602\n"
            "async def ping(url='http://example.com'):\n"
            "    async with httpx.AsyncClient() as cli:\n"
            "        r = await cli.get(url)\n"
            "    return r.status_code\n"
        )
        commit = await _put(
            c,
            f"/repos/{OWNER}/{REPO}/contents/{path}",
            {
                "content": b64(code),
                "message": f"e2e: add vuln_demo {ts}",
                "branch": base_branch,
                "new_branch": branch,
            },
        )
        assert commit.get("content", {}).get("path") == path

        # 3) open a PR
        pr = await _post(
            c,
            f"/repos/{OWNER}/{REPO}/pulls",
            {
                "title": f"E2E PR {ts}: trigger AI reviewer",
                "head": branch,
                "base": base_branch,
                "body": "Automated e2e test PR to trigger AI review.",
            },
        )
        pr_number = pr["number"]

        # 4) poll for AI comment + risk label (up to ~90s)
        comment_found = False
        label_found = False
        deadline = time.time() + 90

        while time.time() < deadline and not (comment_found and label_found):
            # comments
            comments = await _get(c, f"/repos/{OWNER}/{REPO}/issues/{pr_number}/comments")
            comment_found = any("AI Reviewer" in (cm.get("body") or "") for cm in comments)

            # labels
            issue = await _get(c, f"/repos/{OWNER}/{REPO}/issues/{pr_number}")
            labels = [lb.get("name", "").lower() for lb in issue.get("labels", [])]
            label_found = any(lb.startswith("risk: ") for lb in labels)

            if comment_found and label_found:
                break
            await anyio.sleep(5)

        assert comment_found, "AI Reviewer comment not found within timeout"
        assert label_found, "risk label not added within timeout"
