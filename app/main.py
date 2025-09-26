from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os, hmac, hashlib, httpx
from dotenv import load_dotenv
import os
from .llm import review_simple
import textwrap


load_dotenv()

GITEA_BASE = os.getenv("GITEA_BASE", "http://3.252.248.64:3000/api/v1").rstrip("/")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # set this in Gitea webhook

if not GITEA_TOKEN:
    raise RuntimeError("GITEA_TOKEN missing")

app = FastAPI(title="Gitea AI Reviewer", version="0.1.0")

# small GET helper for Gitea API
async def gitea_get(path: str, params: dict | None = None):
    headers = {"Authorization": f"token {os.getenv('GITEA_TOKEN','')}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{GITEA_BASE}{path}", headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()

def _truncate(s: str, max_chars: int = 48000) -> str:
    return s if len(s) <= max_chars else s[:max_chars] + "\n...[truncated]..."

# build meta + unified diff using /pulls/{index}/files (uses 'patch' field)
async def fetch_pr_meta_and_diff(owner: str, repo: str, pr_index: int) -> tuple[dict, str]:
    pr = await gitea_get(f"/repos/{owner}/{repo}/pulls/{pr_index}")
    files = await gitea_get(f"/repos/{owner}/{repo}/pulls/{pr_index}/files")

    meta = {
        "owner": owner,
        "repo": repo,
        "pr": pr_index,
        "title": pr.get("title", ""),
        "body": pr.get("body", "") or "",
        "files": [f.get("filename","") for f in files or []],
    }

    # Build a simple unified-style diff from file patches (if present)
    chunks = []
    for f in files or []:
        fn = f.get("filename", "")
        patch = f.get("patch")
        if patch:
            chunks.append(f"diff --git a/{fn} b/{fn}\n{patch}")
    diff_text = "\n\n".join(chunks) if chunks else ""

    return meta, diff_text

def sig_ok(secret: str, body: bytes, headers) -> bool:
    """Accepts Gitea/Gogs and GitHub signature styles."""
    if not secret:  # allow unsigned for local testing
        return True

    # Gitea/Gogs: X-Gitea-Signature / X-Gogs-Signature (hex; sometimes 'sha256=hex')
    sig = headers.get("X-Gitea-Signature") or headers.get("X-Gogs-Signature")
    if sig:
        sig_hex = sig.split("=", 1)[1] if sig.startswith(("sha256=", "SHA256=")) else sig
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hex, expected)

    # GitHub style (some proxies/tools reuse): sha256=...
    sig256 = headers.get("X-Hub-Signature-256")
    if sig256 and sig256.startswith(("sha256=", "SHA256=")):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig256.split("=", 1)[1], expected)

    # GitHub legacy sha1
    sig1 = headers.get("X-Hub-Signature")
    if sig1 and sig1.startswith(("sha1=", "SHA1=")):
        expected = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        return hmac.compare_digest(sig1.split("=", 1)[1], expected)

    return False

async def gitea_post(path: str, json: dict):
    headers = {"Authorization": f"token {GITEA_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{GITEA_BASE}{path}", headers=headers, json=json)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type","").startswith("application/json") else {}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/webhooks/gitea")
async def gitea_webhook(request: Request):
    body = await request.body()
    # OLD: signature = request.headers.get("X-Gitea-Signature")
    # OLD: if not sig_ok(WEBHOOK_SECRET, body, signature): ...
    if not sig_ok(WEBHOOK_SECRET, body, request.headers):
        raise HTTPException(status_code=401, detail="invalid signature")

    event = request.headers.get("X-Gitea-Event", "")
    payload = await request.json()

    # We react to PR opened/synchronized
    if event == "pull_request":
        action = payload.get("action")
        if action in {"opened", "synchronized", "reopened"}:
            repo = payload["repository"]
            owner = repo["owner"]["login"]
            name = repo["name"]
            pr = payload["pull_request"]
            pr_index = pr["number"]  # aka issue index

            # Build a small prompt from the webhook payload (we'll add diffs next step)
            title = pr.get("title", "")
            body = pr.get("body", "") or "(no description)"
            # Fetch PR context + build prompt with real diff
            meta, diff_text = await fetch_pr_meta_and_diff(owner, name, pr_index)

            prompt = textwrap.dedent(f"""
            Review this pull request:

            Repo: {meta['owner']}/{meta['repo']}
            PR #{meta['pr']}: {meta['title']}
            Author notes:
            {meta['body'] or '(no description)'}
            Files changed ({len(meta['files'])}): {', '.join(meta['files'][:20])}

            Tasks:
            - Summarize the change in 2–4 bullets.
            - Flag potential bugs, security or performance risks (reference file/line if possible).
            - Suggest concrete improvements (short code snippets if helpful).
            - Give a risk level: Low | Medium | High, with 1-line justification.

            Unified diff:
            {_truncate(diff_text)}
            """).strip()

            ai_text = await review_simple(prompt)

            comment = (
                f"🤖 **AI Reviewer**\n"
                f"- PR: #{pr_index} in {owner}/{name}\n"
                f"- Files: {len(meta['files'])}\n\n"
                f"{ai_text}"
            )

            await gitea_post(f"/repos/{owner}/{name}/issues/{pr_index}/comments", {"body": comment})

            return JSONResponse({"ok": True, "posted": "comment"})
    return JSONResponse({"ok": True, "ignored": event})
