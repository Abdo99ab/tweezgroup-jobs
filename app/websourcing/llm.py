"""Tiny Claude JSON helper for the websourcing package (same pattern as app.sourcing)."""
import json
import logging
import re

import requests
from flask import current_app

log = logging.getLogger(__name__)


def claude_json(prompt, max_tokens=1500):
    """Ask Claude for a JSON object; returns dict or None (no key / parse failure)."""
    cfg = current_app.config
    if not cfg.get("ANTHROPIC_API_KEY"):
        return None
    try:
        r = requests.post(
            f"{cfg['ANTHROPIC_API_BASE']}/v1/messages",
            headers={"x-api-key": cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": cfg["SUMMARY_MODEL"], "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:200]}")
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") != "thinking")
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0) if m else text)
    except Exception as exc:
        log.warning("websourcing: Claude call failed: %s", exc)
        return None
