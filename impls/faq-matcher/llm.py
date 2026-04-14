import os
import httpx

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "")
LLM_MODEL    = os.getenv("LLM_MODEL", "llama3-8b-8192")
LLM_TIMEOUT  = int(os.getenv("LLM_TIMEOUT", "30"))


def chat(prompt: str) -> str:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set.")
    resp = httpx.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()
