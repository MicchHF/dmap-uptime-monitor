"""Print the chat ID for the person who opens a one-time bot link."""

from getpass import getpass
import re
from secrets import token_urlsafe
import time

from monitor import TelegramError, telegram


def main():
    token = getpass("Token from BotFather (input is hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Invalid token format.")
    try:
        username = telegram(token, "getMe", {})["username"]
        if telegram(token, "getWebhookInfo", {}).get("url"):
            raise SystemExit("This bot still has a webhook. Use known chat IDs or retire the old integration first.")
        challenge = token_urlsafe(16)
        print(f"Open https://t.me/{username}?start={challenge} and press Start.", flush=True)
        deadline = time.monotonic() + 120
        offset = 0
        while time.monotonic() < deadline:
            updates = telegram(token, "getUpdates", {"offset": offset, "timeout": 2})
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat = message.get("chat", {})
                if chat.get("type") == "private" and message.get("text") == f"/start {challenge}":
                    print(f"TELEGRAM_CHAT_ID={chat['id']}")
                    return
        raise SystemExit("No matching Start received in two minutes. Run the script again.")
    except TelegramError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
