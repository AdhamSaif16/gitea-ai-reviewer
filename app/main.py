from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os, hmac, hashlib, httpx
from dotenv import load_dotenv

load_dotenv()

GITEA_BASE = os.getenv("GITEA_BASE", "http://3.252.248.64:3000/api/v1").rstrip("/")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # set this in Gitea webhook

if not GITEA_TOKEN:
    raise RuntimeError("GITEA_TOKEN missing")

app = FastAPI(title="Gitea AI Reviewer", version="0.1.0")

def sig_ok(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret:  # allow empty secret for local testing
        return True
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + digest, signature)

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
    signature = request.headers.get("X-Gitea-Signature")
    if not sig_ok(WEBHOOK_SECRET, body, signature):
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

            # Minimal placeholder review (LLM comes next)
            summary = (
                f"🤖 AI Reviewer (placeholder)\n"
                f"- PR: #{pr_index} in {owner}/{name}\n"
                f"- Changed files: {pr.get('changed_files', 'n/a')}\n"
                f"- Next step: enable LLM to generate inline findings."
            )

            # Post a PR comment (PRs are issues in Gitea)
            try:
                await gitea_post(f"/repos/{owner}/{name}/issues/{pr_index}/comments", {"body": summary})
            except httpx.HTTPStatusError as e:
                # If comment API fails, surface the error for debugging
                raise HTTPException(status_code=502, detail=e.response.text)

            return JSONResponse({"ok": True, "posted": "comment"})
    return JSONResponse({"ok": True, "ignored": event})
