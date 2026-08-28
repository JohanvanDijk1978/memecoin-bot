"""
alerts.py — Telegram DM alerts for Wallet Groups convergences.

Why the dashboard sends these itself rather than asking the bot to:
memedash is a separate systemd service from memebot, and `wallets.py` already
loads `../.env` at import, which is where `TELEGRAM_BOT_TOKEN` and
`YOUR_TELEGRAM_USER_ID` live. Routing through the bot would mean inventing an
IPC channel between two processes that already both have the credentials. This
is `src/send_ping.py` rewritten on httpx, which the dashboard already depends
on, with the same sendPhoto-then-sendMessage fallback.

Alerts go to Johan's DM only, never to the CA alert group: a convergence is a
rarer and more personal signal than a scraped call, and it should not have to
compete with the firehose.

Nothing here may raise into a scan round. `send()` swallows every failure and
logs it — a Telegram outage must never stop the wallet scanner.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("memedash.alerts")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 10.0

PADRE_SLUG = {"solana": "solana", "ethereum": "eth", "bsc": "bsc",
              "base": "base", "robinhood": "robinhood"}


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _chat() -> str:
    # DM only. TELEGRAM_ALERT_GROUP is deliberately not consulted.
    return os.getenv("WG_ALERT_CHAT", os.getenv("YOUR_TELEGRAM_USER_ID", "")).strip()


def enabled() -> bool:
    """Alerts are on when there is somewhere to send them and nobody said no."""
    if os.getenv("WG_ALERTS", "1").strip() in ("0", "false", "no"):
        return False
    return bool(_token() and _chat())


def status() -> dict:
    """What the page shows in its notes line, so a silent DM is explainable."""
    if os.getenv("WG_ALERTS", "1").strip() in ("0", "false", "no"):
        return {"ok": False, "note": "disabled by WG_ALERTS"}
    if not _token():
        return {"ok": False, "note": "no TELEGRAM_BOT_TOKEN"}
    if not _chat():
        return {"ok": False, "note": "no YOUR_TELEGRAM_USER_ID"}
    return {"ok": True, "note": "DM"}


def padre(address: str, chain_id: str) -> str:
    slug = PADRE_SLUG.get((chain_id or "").lower()) or \
        ("eth" if address.startswith("0x") else "solana")
    return f"https://trade.padre.gg/trade/{slug}/{address}"


def _usd(v) -> str:
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "—"
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= cut:
            return f"${v / cut:.2f}{suffix}"
    return f"${v:,.2f}"


def _escape(s: str) -> str:
    """Markdown (legacy) only needs the characters that open a style run.

    A token called *FREE* MONEY_ would otherwise break the whole message and
    Telegram would reject it, costing the alert entirely.
    """
    return str(s or "").replace("\\", "").replace("*", "").replace("_", "") \
                       .replace("`", "").replace("[", "(").replace("]", ")")


def convergence_text(token: dict, group_name: str, count: int) -> str:
    """The bot's CA-ping shape, with the wallets that make this worth reading."""
    symbol = _escape(token.get("symbol") or "?")
    name = _escape(token.get("name") or "")
    address = token.get("address") or ""
    first = count <= 2
    head = (f"🪙 *{count} wallets now hold ${symbol}*" if not first
            else f"🪙 *Convergence: ${symbol}*")

    lines = [
        head,
        f"_{name}_" if name else "",
        f"*Group:* {_escape(group_name)}",
        f"*CA:* `{address}`",
        f"*Market Cap:* {_usd(token.get('mc'))}",
        f"*Liquidity:* {_usd(token.get('liq'))}",
        f"*Combined:* {_usd(token.get('position_usd'))}"
        + (f" · {token['supply_pct']:.2f}% of supply" if token.get("supply_pct") else ""),
        "",
    ]
    for w in (token.get("wallets") or []):
        supply = f" · {w['supply_pct']:.2f}%" if w.get("supply_pct") is not None else ""
        pnl = ""
        if w.get("pnl_usd") is not None:
            sign = "+" if w["pnl_usd"] >= 0 else "-"
            pnl = f" · {sign}{_usd(abs(w['pnl_usd']))}"
        lines.append(f"• *{_escape(w.get('label') or '?')}* — {_usd(w.get('value_usd'))}{supply}{pnl}")
    lines += ["", f"[Trade on Padre]({padre(address, token.get('chain_id') or '')})"]
    return "\n".join(x for x in lines if x != "" or True).strip()


async def send(client: httpx.AsyncClient, text: str, photo_url: str = "") -> bool:
    """Send one DM. Returns whether Telegram accepted it. Never raises."""
    token, chat = _token(), _chat()
    if not token or not chat:
        return False
    try:
        if photo_url:
            r = await client.post(
                API.format(token=token, method="sendPhoto"),
                json={"chat_id": chat, "photo": photo_url, "caption": text,
                      "parse_mode": "Markdown"},
                timeout=TIMEOUT)
            if r.status_code == 200:
                return True
            # A dead or oversized banner URL is common and must not cost the
            # alert -- fall through to text, exactly as send_ping.py does.
            log.info("telegram sendPhoto refused (%s), falling back to text", r.status_code)
        r = await client.post(
            API.format(token=token, method="sendMessage"),
            json={"chat_id": chat, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("telegram sendMessage failed: %s %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:                       # an outage must not stop a scan
        log.warning("telegram alert failed: %s", e)
        return False
