# app/llm.py
from __future__ import annotations
import os, httpx

SYSTEM = (
    "You are a senior software engineer performing a precise code review. "
    "Be concise, specific, and actionable. Use short bullet points."
)

async def review_simple(prompt_text: str) -> str:
    """
    OpenAI call (reads env at call time so updated .env is respected).
    """
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        return "OpenAI not configured (set OPENAI_API_KEY in .env)."

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
    }

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
        # surface the first 200 chars of error body to help debugging
        body = (e.response.text or "")[:200]
        return f"OpenAI API error: {e.response.status_code} {body}"
    except Exception as e:
        return f"OpenAI client error: {e}"
