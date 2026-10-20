"""Check Gemini API connectivity and list available models.

Loads .env, lists models that support generateContent, performs one tiny
verification call with the configured model, and prints OK/FAIL without leaking the API key.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path when invoked directly as a script
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import _load_env_file
from backend.gemini_client import sanitize_sensitive_text


def check_gemini(load_env: bool = True) -> int:
    """Verify Gemini configuration and connectivity."""
    if load_env:
        _load_env_file(REPO_ROOT / ".env")

    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("IBVAP_GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        print(f"FAIL: {model_name} (GEMINI_API_KEY not configured)")
        return 1

    try:
        from google import genai

        client = genai.Client(api_key=api_key)

        # List models supporting generateContent
        print("Discovering available Gemini models...")
        models = client.models.list()
        generate_models: list[str] = []
        for m in models:
            actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None)
            name = getattr(m, "name", str(m))
            if actions:
                if any("generateContent" in str(a) for a in actions):
                    generate_models.append(name)
            else:
                generate_models.append(name)

        if generate_models:
            print(f"Found {len(generate_models)} model(s) supporting content generation.")
        else:
            print("No models explicitly listed generateContent.")

        # Perform one small test call
        response = client.models.generate_content(
            model=model_name,
            contents="Respond with OK if received.",
        )
        if response and response.text:
            print(f"OK: {model_name}")
            return 0

        print(f"FAIL: {model_name} (empty response)")
        return 1

    except Exception as exc:  # noqa: BLE001
        sanitized = sanitize_sensitive_text(str(exc))
        print(f"FAIL: {model_name} ({sanitized})")
        return 1


if __name__ == "__main__":
    sys.exit(check_gemini())
