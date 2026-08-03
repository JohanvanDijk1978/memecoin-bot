"""
diag_combined_channel.py
────────────────────────
Pinpoints why the combined Solana+EVM dex channel isn't receiving alerts.

Run on the VPS from the repo root:
    cd /root/memecoin-bot && python3 tools/diag_combined_channel.py

Checks, in the order they can fail:
  1. Is DEX_UPDATES_COMBINED_CHANNEL_ID actually loaded from .env?
  2. Does the deployed code contain the combined-channel wiring?
  3. Is the bot token valid?
  4. Is the channel reachable (correct ID)?
  5. Is the bot an admin there with post permission?
  6. Does a real send succeed (text + photo, same paths the watcher uses)?

Read-only except step 6, which posts two test messages to the channel.
"""

import os
import sys
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

OK   = "\033[92m✓\033[0m"
BAD  = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def api(token: str, method: str, payload: dict = None):
    """Call the Telegram Bot API. Returns the parsed JSON body either way —
    Telegram puts the useful failure reason in `description`, and urllib
    raises on 4xx, so we have to read the error body explicitly."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "description": f"{e!r}"}


def main() -> int:
    fail = 0

    # ── 1. env ────────────────────────────────────────────────────────────
    print("\n[1] Environment")
    token    = os.getenv("TELEGRAM_BOT_TOKEN", "")
    combined = os.getenv("DEX_UPDATES_COMBINED_CHANNEL_ID", "")
    sol      = os.getenv("DEX_UPDATES_CHANNEL_ID", "")
    evm      = os.getenv("DEX_UPDATES_EVM_CHANNEL_ID", "")

    print(f"  {OK if token else BAD} TELEGRAM_BOT_TOKEN         "
          f"{'set' if token else 'MISSING'}")
    print(f"  {OK if sol else WARN} DEX_UPDATES_CHANNEL_ID     {sol or '(unset)'}")
    print(f"  {OK if evm else WARN} DEX_UPDATES_EVM_CHANNEL_ID {evm or '(unset)'}")
    print(f"  {OK if combined else BAD} DEX_UPDATES_COMBINED_CHANNEL_ID "
          f"{combined or 'MISSING'}")

    if not combined:
        print("\n  → The variable isn't in this .env. Add it and restart:")
        print("      echo 'DEX_UPDATES_COMBINED_CHANNEL_ID=-1003922902979' >> .env")
        print("      systemctl restart memebot")
        return 1
    if not token:
        return 1

    # ── 2. deployed code ──────────────────────────────────────────────────
    print("\n[2] Deployed code")
    for mod in ("src/dex_watcher.py", "src/dex_watcher_evm.py",
                "src/dex_milestone_tracker.py"):
        try:
            with open(mod) as f:
                body = f.read()
            has = "COMBINED_CHANNEL_ID" in body or "tg_combined" in body
            print(f"  {OK if has else BAD} {mod:<32} "
                  f"{'wired' if has else 'OLD CODE — git pull needed'}")
            fail += 0 if has else 1
        except Exception as e:
            print(f"  {BAD} {mod:<32} unreadable: {e}")
            fail += 1

    # ── 3. token ──────────────────────────────────────────────────────────
    print("\n[3] Bot token")
    me = api(token, "getMe")
    if not me.get("ok"):
        print(f"  {BAD} getMe failed: {me.get('description')}")
        return 1
    bot = me["result"]
    bot_id = bot["id"]
    print(f"  {OK} @{bot.get('username')} (id {bot_id})")

    # ── 4. channel reachable ──────────────────────────────────────────────
    print("\n[4] Channel reachable")
    chat = api(token, "getChat", {"chat_id": combined})
    if not chat.get("ok"):
        desc = chat.get("description", "")
        print(f"  {BAD} getChat failed: {desc}")
        if "not found" in desc.lower():
            print("  → Wrong ID, or the bot was never added to the channel.")
            print("    Add the bot as an admin, then re-run this script.")
        return 1
    c = chat["result"]
    print(f"  {OK} {c.get('title')!r} (type={c.get('type')})")

    # ── 5. admin rights ───────────────────────────────────────────────────
    print("\n[5] Bot permissions")
    mem = api(token, "getChatMember", {"chat_id": combined, "user_id": bot_id})
    if not mem.get("ok"):
        print(f"  {BAD} getChatMember failed: {mem.get('description')}")
        fail += 1
    else:
        m = mem["result"]
        status = m.get("status")
        # Channels require admin + can_post_messages; groups only need membership.
        can_post = m.get("can_post_messages")
        is_admin = status in ("administrator", "creator")
        print(f"  {OK if is_admin else BAD} status = {status}")
        if c.get("type") == "channel":
            print(f"  {OK if can_post else BAD} can_post_messages = {can_post}")
            if not can_post:
                print("  → Channel > Administrators > your bot > enable 'Post Messages'")
                fail += 1
        if not is_admin:
            print("  → Add the bot as an administrator of the channel.")
            fail += 1

    # ── 6. real send ──────────────────────────────────────────────────────
    print("\n[6] Live send test")
    r = api(token, "sendMessage", {
        "chat_id": combined,
        "text": "🪙 diag: combined channel text send OK",
        "disable_web_page_preview": True,
    })
    if r.get("ok"):
        print(f"  {OK} sendMessage → message_id {r['result']['message_id']}")
    else:
        print(f"  {BAD} sendMessage failed: {r.get('description')}")
        fail += 1

    # The watcher prefers sendPhoto when a banner exists; a channel can pass
    # the text check but still reject media, which would drop every alert
    # that has a header image.
    r2 = api(token, "sendPhoto", {
        "chat_id": combined,
        "photo": "https://dd.dexscreener.com/ds-data/tokens/solana/"
                 "So11111111111111111111111111111111111111112/header.png",
        "caption": "🪙 diag: combined channel photo send OK",
    })
    if r2.get("ok"):
        print(f"  {OK} sendPhoto   → message_id {r2['result']['message_id']}")
    else:
        print(f"  {WARN} sendPhoto failed: {r2.get('description')}")
        print("  → Alerts would still land via the text fallback.")

    # ── verdict ───────────────────────────────────────────────────────────
    print()
    if fail:
        print(f"{BAD} {fail} problem(s) above — fix, then: systemctl restart memebot")
    else:
        print(f"{OK} Config is good. If alerts still don't appear, the watchers")
        print("  simply haven't fired yet — they only alert on NEW paid Dexscreener")
        print("  updates for pairs older than 24h (SOL) / 12h (EVM). Watch with:")
        print("    journalctl -u memebot -f | grep -i 'dex_watcher'")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
