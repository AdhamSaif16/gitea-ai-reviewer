from __future__ import annotations
import os, httpx, asyncio

SYSTEM = (
    "You are a senior software engineer performing a precise code review. "
    "Be concise, specific, and actionable. Use short bullet points."
)

def _read_secret_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            val = f.read().strip()
            return val or None
    except FileNotFoundError:
        return None

async def review_simple(prompt_text: str) -> str:
    # Prefer env, fallback to secret file
    api_key = (os.getenv("OPENAI_API_KEY") or _read_secret_file("/run/secrets/openai_api_key") or "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return "OpenAI not configured (set OPENAI_API_KEY or /run/secrets/openai_api_key)."

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
    }

    # light retry for transient 429 rate-limit (not quota)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"].strip()
        except httpx.HTTPStatusError as e:
            body = (e.response.text or "")[:200]
            if e.response.status_code == 429 and "quota" not in body.lower():
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return f"OpenAI API error: {e.response.status_code} {body}"
        except Exception as ex:
            return f"OpenAI client error: {ex}"
    return "OpenAI rate limited repeatedly — try again shortly."
