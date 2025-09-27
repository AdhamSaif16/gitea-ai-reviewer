# app/main.py
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import os, hmac, hashlib, httpx, textwrap, re, logging
from dotenv import load_dotenv
from .llm import review_simple

load_dotenv()

GITEA_BASE = os.getenv("GITEA_BASE", "http://54.229.37.166:3000/api/v1").rstrip("/")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # set this in the Gitea webhook

if not GITEA_TOKEN:
    raise RuntimeError("GITEA_TOKEN missing")

app = FastAPI(title="Gitea AI Reviewer", version="0.2.0")

# basic logs
logger = logging.getLogger("ai-reviewer")
logging.basicConfig(level=logging.INFO)

def _read_secret_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return v or None
    except FileNotFoundError:
        return None

def _get_secret(env_name: str, *file_paths: str) -> str:
    v = os.getenv(env_name)
    if v:
        return v
    for p in file_paths:
        v = _read_secret_file(p)
        if v:
            return v
    return ""


# ----------------- Gitea helpers -----------------

async def gitea_get(path: str, params: dict | None = None):
    headers = {"Authorization": f"token {GITEA_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{GITEA_BASE}{path}", headers=headers, params=params or {})
        r.raise_for_status()
        return r.json()

async def gitea_post(path: str, json_data):
    headers = {"Authorization": f"token {GITEA_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{GITEA_BASE}{path}", headers=headers, json=json_data)
        r.raise_for_status()
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

async def gitea_post_json(path: str, json_data):
    return await gitea_post(path, json_data)

async def ensure_label(owner: str, repo: str, name: str, color: str, desc: str = "") -> dict:
    # Find or create a repository label
    labels = await gitea_get(f"/repos/{owner}/{repo}/labels")
    for lb in labels:
        if lb.get("name", "").lower() == name.lower():
            return lb
    return await gitea_post_json(
        f"/repos/{owner}/{repo}/labels",
        {"name": name, "color": color.lstrip("#"), "description": desc},
    )

async def add_label_to_issue(owner: str, repo: str, issue_index: int, label_id: int):
    # Some Gitea versions expect a list of IDs; others accept {"labels":[ids]}
    try:
        await gitea_post_json(f"/repos/{owner}/{repo}/issues/{issue_index}/labels", [label_id])
    except httpx.HTTPStatusError:
        await gitea_post_json(f"/repos/{owner}/{repo}/issues/{issue_index}/labels", {"labels": [label_id]})


# ----------------- Diff & prompt helpers -----------------

def _truncate(s: str, max_chars: int = 48_000) -> str:
    return s if len(s) <= max_chars else s[:max_chars] + "\n...[truncated]..."

async def fetch_pr_meta_and_diff(owner: str, repo: str, pr_index: int) -> tuple[dict, str]:
    """Collect PR meta + build a unified-ish diff from file patches."""
    pr = await gitea_get(f"/repos/{owner}/{repo}/pulls/{pr_index}")
    files = await gitea_get(f"/repos/{owner}/{repo}/pulls/{pr_index}/files")

    meta = {
        "owner": owner,
        "repo": repo,
        "pr": pr_index,
        "title": pr.get("title", ""),
        "body": pr.get("body", "") or "",
        "files": [f.get("filename", "") for f in files or []],
    }

    chunks = []
    for f in files or []:
        fn = f.get("filename", "")
        patch = f.get("patch")
        if patch:
            chunks.append(f"diff --git a/{fn} b/{fn}\n{patch}")
    diff_text = "\n\n".join(chunks) if chunks else ""

    return meta, diff_text


# ----------------- Signature verify -----------------

def sig_ok(secret: str, body: bytes, headers) -> bool:
    """Accept Gitea/Gogs (hex or 'sha256=hex') and GitHub (sha256/sha1) signatures."""
    if not secret:  # allow unsigned for local testing
        return True

    # Gitea/Gogs
    sig = headers.get("X-Gitea-Signature") or headers.get("X-Gogs-Signature")
    if sig:
        sig_hex = sig.split("=", 1)[1] if sig.startswith(("sha256=", "SHA256=")) else sig
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig_hex, expected)

    # GitHub modern
    sig256 = headers.get("X-Hub-Signature-256")
    if sig256 and sig256.startswith(("sha256=", "SHA256=")):
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig256.split("=", 1)[1], expected)

    # GitHub legacy
    sig1 = headers.get("X-Hub-Signature")
    if sig1 and sig1.startswith(("sha1=", "SHA1=")):
        expected = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        return hmac.compare_digest(sig1.split("=", 1)[1], expected)

    return False


# ----------------- Routes -----------------

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/webhooks/gitea")
async def gitea_webhook(request: Request):
    raw = await request.body()
    if not sig_ok(WEBHOOK_SECRET, raw, request.headers):
        raise HTTPException(status_code=401, detail="invalid signature")

    event = request.headers.get("X-Gitea-Event", "")
    payload = await request.json()

    if event == "pull_request":
        action = payload.get("action")
        if action in {"opened", "synchronized", "reopened"}:
            repo = payload["repository"]
            owner = repo["owner"]["login"]
            name = repo["name"]
            pr = payload["pull_request"]
            pr_index = pr["number"]

            logger.info("webhook: PR %s action=%s repo=%s/%s", pr_index, action, owner, name)

            # Build prompt with real diff
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
            logger.info("posted AI comment on PR #%s", pr_index)

            # Parse "risk" from AI text and apply label
            risk = "medium"
            m = re.search(r"risk(?: level)?\s*:\s*(low|medium|high)", ai_text, re.I)
            if m:
                risk = m.group(1).lower()

            label_map = {
                "low":   ("risk: low",    "#2ea043"),
                "medium":("risk: medium", "#fbca04"),
                "high":  ("risk: high",   "#d73a4a"),
            }
            label_name, label_color = label_map[risk]

            lb = await ensure_label(owner, name, label_name, label_color, "AI reviewer assessed risk")
            await add_label_to_issue(owner, name, pr_index, lb["id"])
            logger.info("applied label '%s' to PR #%s", label_name, pr_index)

            return JSONResponse({"ok": True, "posted": "comment+label"})

    return JSONResponse({"ok": True, "ignored": event})
