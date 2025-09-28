import os, base64, time
import httpx
import pytest
import anyio
from httpx import HTTPStatusError

GITEA_BASE = os.getenv("GITEA_BASE", "http://localhost:3000/api/v1").rstrip("/")
OWNER = os.getenv("OWNER", "gitea")
REPO = os.getenv("REPO", "ai-review-demo")

def _load_token() -> str:
    tok = os.getenv("GITEA_TOKEN")
    if tok:
        return tok.strip()
    for p in ("/run/secrets/gitea_token", os.path.expanduser("~/stack/secrets/gitea_token")):
        if os.path.exists(p):
            return open(p, "r", encoding="utf-8").read().strip()
    pytest.skip("GITEA_TOKEN missing")
TOKEN = _load_token()
HDRS = {"Authorization": f"token {TOKEN}"}

# Force anyio to use asyncio backend only
@pytest.fixture
def anyio_backend():
    return "asyncio"

async def _get(c: httpx.AsyncClient, path: str, **params):
    r = await c.get(f"{GITEA_BASE}{path}", headers=HDRS, params=params)
    r.raise_for_status()
    return r.json()

async def _post(c: httpx.AsyncClient, path: str, payload: dict):
    r = await c.post(f"{GITEA_BASE}{path}", headers=HDRS, json=payload)
    r.raise_for_status()
    return r.json()

async def _put(c: httpx.AsyncClient, path: str, payload: dict):
    r = await c.put(f"{GITEA_BASE}{path}", headers=HDRS, json=payload)
    if r.status_code >= 400:
        raise HTTPStatusError(f"{r.status_code} {r.reason_phrase}: {r.text}", request=r.request, response=r)
    return r.json()

def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

async def ensure_base_branch(c: httpx.AsyncClient, base_branch: str) -> None:
    # ensure base branch exists (create initial commit if needed)
    try:
        await _get(c, f"/repos/{OWNER}/{REPO}/branches/{base_branch}")
        return
    except HTTPStatusError as e:
        if e.response.status_code != 404:
            raise
    await _put(
        c,
        f"/repos/{OWNER}/{REPO}/contents/README.md",
        {"content": b64("# ai-review-demo\n\ninitial commit\n"),
         "message": "chore: initial commit",
         "branch": base_branch},
    )

async def create_branch(c: httpx.AsyncClient, new_branch: str, from_branch: str) -> None:
    # Try branches API
    try:
        await _post(
            c,
            f"/repos/{OWNER}/{REPO}/branches",
            {"new_branch_name": new_branch, "old_branch_name": from_branch},
        )
        return
    except HTTPStatusError as e:
        if e.response is None or e.response.status_code not in (404, 422):
            raise
    # Fallback: git refs API
    base = await _get(c, f"/repos/{OWNER}/{REPO}/branches/{from_branch}")
    sha = base["commit"]["id"] if "commit" in base else base["commit"]["sha"]
    await _post(c, f"/repos/{OWNER}/{REPO}/git/refs", {"ref": f"refs/heads/{new_branch}", "sha": sha})

async def get_file_sha(c: httpx.AsyncClient, branch: str, path: str) -> str | None:
    # returns sha if file exists on branch; otherwise None
    try:
        item = await _get(c, f"/repos/{OWNER}/{REPO}/contents/{path}", ref=branch)
        # Gitea/GitHub style may use "sha" or nested "content.sha"
        return item.get("sha") or item.get("content", {}).get("sha")
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise

@pytest.mark.anyio
async def test_ai_reviewer_end_to_end(anyio_backend):
    async with httpx.AsyncClient(timeout=30) as c:
        # discover & ensure base branch
        repo = await _get(c, f"/repos/{OWNER}/{REPO}")
        base_branch = (repo.get("default_branch") or "main").strip()
        await ensure_base_branch(c, base_branch)

        # create feature branch
        ts = int(time.time())
        branch = f"e2e-ai-{ts}"
        await create_branch(c, branch, base_branch)

        # commit a file on the new branch (create-or-update with sha if needed)
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
        payload = {"content": b64(code), "message": f"e2e: add vuln_demo {ts}", "branch": branch}
        sha = await get_file_sha(c, branch, path)
        if sha:
            payload["sha"] = sha  # update existing file
        commit = await _put(c, f"/repos/{OWNER}/{REPO}/contents/{path}", payload)
        assert commit.get("content", {}).get("path") == path

        # open PR
        pr = await _post(
            c,
            f"/repos/{OWNER}/{REPO}/pulls",
            {"title": f"E2E PR {ts}: trigger AI reviewer", "head": branch, "base": base_branch,
             "body": "Automated e2e test PR to trigger AI review."},
        )
        pr_number = pr["number"]

        # poll for AI comment + risk label
        comment_found = False
        label_found = False
        deadline = time.time() + 120

        while time.time() < deadline and not (comment_found and label_found):
            comments = await _get(c, f"/repos/{OWNER}/{REPO}/issues/{pr_number}/comments")
            comment_found = any("AI Reviewer" in (cm.get("body") or "") for cm in comments)

            issue = await _get(c, f"/repos/{OWNER}/{REPO}/issues/{pr_number}")
            labels = [lb.get("name", "").lower() for lb in issue.get("labels", [])]
            label_found = any(lb.startswith("risk: ") for lb in labels)

            if comment_found and label_found:
                break
            await anyio.sleep(5)

        assert comment_found, "AI Reviewer comment not found within timeout"
        assert label_found, "risk label not added within timeout"
