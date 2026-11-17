"""
Helper script to generate a reusable Telethon StringSession.
Use this once locally to log into your Telegram account, then you can copy
the generated string session into Fly.io secrets (or local .env) so that the
cloud container authenticates automatically without prompting for SMS codes.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("Telethon is required. Run: pip install telethon")
    sys.exit(1)

def main():
    print("=" * 60)
    print("TELEGRAM STRING SESSION GENERATOR")
    print("=" * 60)

    api_id_env = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash_env = os.getenv("TELEGRAM_API_HASH", "").strip()

    if not api_id_env:
        api_id_env = input("Enter your Telegram API_ID (from my.telegram.org): ").strip()
    if not api_hash_env:
        api_hash_env = input("Enter your Telegram API_HASH: ").strip()

    if not api_id_env or not api_hash_env:
        print("API_ID and API_HASH are required.")
        sys.exit(1)

    with TelegramClient(StringSession(), int(api_id_env), api_hash_env) as client:
        session_str = client.session.save()
        print("\n" + "=" * 60)
        print("LOGIN SUCCESSFUL!")
        print("=" * 60)
        print("\nYour TELEGRAM_STRING_SESSION string is:\n")
        print(session_str)
        print("\n" + "=" * 60)
        print("FLY.IO DEPLOYMENT INSTRUCTION:")
        print("Set this string session in Fly.io secrets so the bot is authenticated in the cloud:")
        print(f'\nfly secrets set TELEGRAM_API_ID={api_id_env} TELEGRAM_API_HASH="{api_hash_env}" TELEGRAM_STRING_SESSION="{session_str}"\n')
        print("=" * 60)

if __name__ == "__main__":
    main()
