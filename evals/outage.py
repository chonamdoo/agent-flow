"""Usage-quota exhaustion and model-service outages are infrastructure, not results (#409 v3.14 E, `infra-outage`).

When the account's usage limit is reached, the claude CLI ends the call instead of the work: the controller's stream
carries a `rate_limit_event` whose `rate_limit_info.status` is `rejected` (with no overage to fall back on), an
assistant message with `error: "rate_limit"` / `"billing_error"` (the CLI's own synthetic message, e.g. "You've hit
your limit · resets 3pm", "Claude AI usage limit reached", "API Error: Request rejected (429)"), and a `result` row
with `is_error`. In auto mode the CLI also stops a turn with "Auto mode is unavailable: the server returned no safety
verdict for N responses in a row" when the permission classifier service does not answer. Either way the attempt
measured the service, not the arm.

`from_stream(rows)` and `from_text(text)` return a short detail or None. `drive` checks the controller's stream after
every turn and the texts the kit's reviewers (usage wrapper rows), arm R's router, the simulated user and the review
classifier left; a hit ends the drive, records `harness_error: "infra-outage: <detail>"` and writes the marker
`<runs root>/QUOTA_EXHAUSTED` (`mark`), and the `drive` command exits `EXIT_CODE` so a campaign runner can pause until
the quota resets. The marker is removed by the operator, not by the harness.

Only the CLI's own error surfaces are read (error results, synthetic error messages, rate-limit events, tool results
marked as errors), never the agent's prose: a product with a rate limiter must not look like an outage.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

MARKER = "QUOTA_EXHAUSTED"
EXIT_CODE = 75  # EX_TEMPFAIL
QUOTA = re.compile(r"you(?:'|’)ve hit your\b[^.\n]*limit|you(?:'|’)ve reached your\b[^.\n]*limit"
                   r"|you(?:'|’)re out of (?:extra )?usage|your org is out of usage|usage (?:credit )?limit reached"
                   r"|usage limit (?:has been )?(?:reached|exceeded)|rate_limit_error|request rejected \(429\)"
                   r"|\b429\b[^\n]{0,80}rate.?limit|spend limit reached|credit balance (?:is )?too low", re.I)
SAFETY = re.compile(r"auto mode is unavailable[^\n]{0,40}no safety verdict", re.I)
ERRORS = ("rate_limit", "billing_error")


def from_text(text: str | None) -> str | None:
    """`usage-limit: …` or `auto-mode: …` for a CLI error text that says so, else None."""
    if not text:
        return None
    text = str(text)
    for kind, pattern in (("usage-limit", QUOTA), ("auto-mode", SAFETY)):
        found = pattern.search(text)
        if found:
            start = max(0, found.start() - 40)
            return f"{kind}: {' '.join(text[start:found.end() + 80].split())[:200]}"
    return None


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                out += _texts(block.get("text") if "text" in block else block.get("content"))
            elif isinstance(block, str):
                out.append(block)
        return out
    return []


def from_stream(rows: list[dict]) -> str | None:
    """The first outage the CLI reported in a `stream-json` (or transcript) row list, else None."""
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "rate_limit_event":
            info = row.get("rate_limit_info") if isinstance(row.get("rate_limit_info"), dict) else {}
            if info.get("status") == "rejected" and info.get("overageStatus") not in ("allowed", "allowed_warning"):
                return (f"usage-limit: rate_limit_event rejected ({info.get('rateLimitType') or '?'}"
                        f"{', resets ' + str(info['resetsAt']) if info.get('resetsAt') else ''})")
        elif kind == "assistant":
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            text = " ".join(_texts(message.get("content")))
            if row.get("error") in ERRORS or row.get("apiError") == "usage_limit_reached":
                return from_text(text) or f"usage-limit: assistant error {row.get('error')}: {text[:160]}"
            if message.get("model") == "<synthetic>":
                found = from_text(text)
                if found:
                    return found
        elif kind == "user":
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    found = SAFETY.search(" ".join(_texts(block.get("content"))))
                    if found:
                        return from_text(" ".join(_texts(block.get("content"))))
        elif kind == "result" and row.get("is_error"):
            found = from_text(row.get("result") if isinstance(row.get("result"), str) else None)
            for item in row.get("errors") or []:
                found = found or from_text(str(item))
            if found:
                return found
    return None


def mark(root: Path, run: Path, detail: str) -> Path:
    """Writes (or appends to) `<root>/QUOTA_EXHAUSTED`: one JSON line per run that met the outage."""
    path = root / MARKER
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"run": str(run), "detail": detail,
                              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}) + "\n")
    return path
