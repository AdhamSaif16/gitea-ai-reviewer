# app/llm.py
from __future__ import annotations
import os, httpx

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # set in .env next step
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

SYSTEM = (
    "You are a senior software engineer performing a precise code review. "
    "Be concise, specific, and actionable. Use short bullet points."
)

async def review_simple(prompt_text: str) -> str:
    """
    Call OpenAI Chat Completions and return the review text.
    (We’ll pass real PR context in the next step.)
    """
    if not OPENAI_API_KEY:
        return "OpenAI not configured (set OPENAI_API_KEY in .env)."

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": 0.2,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json=payload,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
