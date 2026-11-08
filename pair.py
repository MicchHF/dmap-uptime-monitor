"""Verify private Telegram chat IDs with one-time commands."""

import argparse
from getpass import getpass
import re
from secrets import token_urlsafe
import time

from monitor import TelegramError, telegram


def pair_chats(token, username, labels):
    codes = {label: token_urlsafe(16) for label in labels}
    for label, code in codes.items():
        print(f"{label}: send this exact command in your private chat with @{username}:", flush=True)
        print(f"/start {code}", flush=True)
    print("Waiting up to 10 minutes. Leave this window open.", flush=True)

    deadline = time.monotonic() + 600
    offset = 0
    paired = {}
    labels_by_code = {code: label for label, code in codes.items()}
    while len(paired) < len(codes) and time.monotonic() < deadline:
        updates = telegram(token, "getUpdates", {"offset": offset, "timeout": 2})
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message", {})
            chat = message.get("chat", {})
            parts = message.get("text", "").split()
            if (
                chat.get("type") != "private"
                or len(parts) != 2
                or parts[0].lower() not in ("/start", f"/start@{username.lower()}")
            ):
                continue
            label = labels_by_code.get(parts[1])
            if label is None or label in paired:
                continue
            chat_id = chat["id"]
            if chat_id in paired.values():
                print(f"{label}: this account was already verified. Use the other account.", flush=True)
                continue
            paired[label] = chat_id
            print(f"{label}: verified.", flush=True)

    if len(paired) < len(codes):
        missing = ", ".join(label for label in labels if label not in paired)
        raise SystemExit(f"No matching command from: {missing}. Run the script again.")
    return [paired[label] for label in labels]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--two", action="store_true", help="verify two different accounts in one run")
    args = parser.parse_args()
    token = getpass("Token from BotFather (input is hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Invalid token format.")
    try:
        username = telegram(token, "getMe", {})["username"]
        if telegram(token, "getWebhookInfo", {}).get("url"):
            raise SystemExit("This bot still has a webhook. Use known chat IDs or retire the old integration first.")
        chat_ids = pair_chats(token, username, ("You", "Dasha") if args.two else ("Recipient",))
    except TelegramError as error:
        raise SystemExit(str(error)) from None
    key = "TELEGRAM_CHAT_IDS" if args.two else "TELEGRAM_CHAT_ID"
    print(f"{key}=" + ",".join(map(str, chat_ids)), flush=True)


if __name__ == "__main__":
    main()
