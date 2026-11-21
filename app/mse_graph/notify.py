"""Send detected changes to a webhook and/or Telegram.

    ALERT_WEBHOOK_URL    POST {"text", "content", "changes"} as JSON. "text" suits Slack
                         incoming webhooks, "content" suits Discord, and "changes" holds
                         the structured records for anything else.
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_ID     chat, group or channel id that the bot can post to
    ALERT_MIN_PERCENT    smallest stake move (percentage points) worth an alert, default 1.0.
                         Crossing 5/10/20/25/33/50/75% always alerts.
    ALERT_MAX_ITEMS      most changes listed in one message, default 40

Delivery failures are logged and never raised: an alert outage must not fail a scrape.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

from .history import is_significant

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000   # Telegram allows 4096 characters per message


def _pct(v) -> str:
    return "?" if v is None else f"{v:g}%"


def describe(c: dict) -> str:
    """One line in Mongolian for a change record."""
    k, who = c["kind"], c.get("entity", "")
    flag = ""
    if c.get("crossed"):
        flag = " ⚑ " + ", ".join(f"{t:g}%" for t in c["crossed"])
    if k == "stake_changed":
        return f"{who}: {_pct(c['old'])} → {_pct(c['new'])} ({c['delta']:+g}){flag}"
    if k == "holder_added":
        return f"Шинэ эзэмшигч: {who} {_pct(c['new'])}{flag}"
    if k == "holder_removed":
        return f"Эзэмшигчдийн жагсаалтаас гарсан: {who} (өмнө {_pct(c['old'])}){flag}"
    if k == "position_added":
        return f"Томилогдсон: {who} — {c['new']}"
    if k == "position_removed":
        return f"Албан тушаалаас гарсан: {who} — {c['old']}"
    if k == "position_changed":
        return f"Албан тушаал өөрчлөгдсөн: {who}: {c['old']} → {c['new']}"
    if k == "company_added":
        return "Жагсаалтад шинээр нэмэгдсэн компани"
    if k == "company_removed":
        return "Жагсаалтаас хасагдсан компани"
    if k == "data_missing":
        return f"Өгөгдөл олдсонгүй ({c.get('section')}, өмнө {c.get('count')} мөр)"
    return k


def format_message(changes: list[dict], max_items: int | None = None) -> str:
    """Changes grouped by company, most changed companies first."""
    max_items = max_items or int(os.getenv("ALERT_MAX_ITEMS", "40"))
    by_co: dict[str, list[dict]] = defaultdict(list)
    for c in changes:
        by_co[c["ticker"]].append(c)
    lines = [f"MSE граф: {len(changes)} өөрчлөлт, {len(by_co)} компани"]
    shown = 0
    for t, cs in sorted(by_co.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if shown >= max_items:
            break
        lines.append(f"\n{t} · {cs[0].get('company', t)}")
        for c in cs:
            if shown >= max_items:
                break
            lines.append("  • " + describe(c))
            shown += 1
    if shown < len(changes):
        lines.append(f"\n… бас {len(changes) - shown} өөрчлөлт")
    return "\n".join(lines)


def _chunks(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:           # a single very long line
            out.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send(changes: list[dict], *, min_percent: float | None = None, client=None) -> dict:
    """Send the significant changes to every configured channel.

    Returns {"sent": n, "channels": [...], "errors": [...]}.
    """
    picked = [c for c in changes if is_significant(c, min_percent)]
    webhook = os.getenv("ALERT_WEBHOOK_URL", "")
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    result = {"sent": len(picked), "channels": [], "errors": []}
    if not picked or not (webhook or (tg_token and tg_chat)):
        result["sent"] = 0
        return result

    text = format_message(picked)
    own = client is None
    if own:
        import httpx
        client = httpx.Client(timeout=30)
    try:
        if webhook:
            try:
                r = client.post(webhook, json={"text": text, "content": text[:2000], "changes": picked})
                r.raise_for_status()
                result["channels"].append("webhook")
            except Exception as exc:          # noqa: BLE001 - never fail the pipeline
                log.error("alert webhook failed: %s", exc)
                result["errors"].append(f"webhook: {exc}")
        if tg_token and tg_chat:
            try:
                for part in _chunks(text, TELEGRAM_LIMIT):
                    r = client.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                                    json={"chat_id": tg_chat, "text": part, "disable_web_page_preview": True})
                    r.raise_for_status()
                result["channels"].append("telegram")
            except Exception as exc:          # noqa: BLE001
                # httpx errors include the URL, which contains the bot token.
                log.error("alert telegram failed: %s", str(exc).replace(tg_token, "***"))
                result["errors"].append("telegram: " + str(exc).replace(tg_token, "***"))
    finally:
        if own:
            client.close()
    return result
