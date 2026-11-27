"""
Helper script to generate a reusable Telethon StringSession.
Supports QR Code login (bypasses SMS/OTP delivery blocks) and Phone+Code login
with explicit delivery-channel diagnostics and SMS resend fallback.
"""
import asyncio
import getpass
import os
from pathlib import Path
import sys
import tempfile
from typing import Optional
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")

try:
    from telethon import TelegramClient, errors, functions
    from telethon.sessions import StringSession
except ImportError:
    print("Telethon is required. Run: pip install telethon")
    sys.exit(1)

try:
    import qrcode
except ImportError:
    qrcode = None


def describe_sent_code_type(code_type) -> str:
    """Translates Telegram's SentCodeType into where the user should check for the code."""
    name = code_type.__class__.__name__ if code_type else "Unknown"
    mapping = {
        "SentCodeTypeApp": "Telegram in-app message (check the official 'Telegram' chat / Archived chats on any logged-in device — NOT SMS).",
        "SentCodeTypeSms": "SMS text message to your phone.",
        "SentCodeTypeCall": "Automated phone call from Telegram.",
        "SentCodeTypeFlashCall": "Flash phone call (last digits of incoming number).",
        "SentCodeTypeMissedCall": "Missed phone call (number suffix).",
        "SentCodeTypeEmailCode": "Email verification code.",
        "SentCodeTypeFragmentSms": "Fragment / anonymous number SMS.",
        "SentCodeTypeFirebaseSms": "Firebase push/SMS verification (mobile app only).",
        "SentCodeTypeSetUpEmailRequired": "Telegram requires setting up/confirming an email first (use QR Code login instead).",
    }
    return f"{name}: {mapping.get(name, 'Check your Telegram app or phone.')}"


def render_qr_code(url: str, png_path: Path, open_viewer: bool = False) -> Optional[Path]:
    """Prints a QR code in the terminal and saves a local PNG for clean phone scanning."""
    if qrcode is None:
        print("\nInstall 'qrcode' for visual QR display (`pip install qrcode pillow`), or open:")
        print(f"  {url}\n")
        return None

    qr_obj = qrcode.QRCode(border=2, box_size=10)
    qr_obj.add_data(url)
    qr_obj.make(fit=True)

    print("\nScan this QR code in Telegram -> Settings -> Devices -> Link Desktop Device:\n")
    try:
        qr_obj.print_ascii(invert=True)
    except UnicodeEncodeError:
        # Older Windows terminal codepages may choke on UTF-8 half-block chars
        matrix = qr_obj.get_matrix()
        for row in matrix:
            print("".join("  " if cell else "##" for cell in row))

    try:
        img = qr_obj.make_image(fill_color="black", back_color="white")
        img.save(png_path)
        print(f"\nSaved crisp QR image to: {png_path}")
        if open_viewer and sys.platform == "win32":
            os.startfile(str(png_path))
        return png_path
    except Exception as e:
        print(f"(Could not open PNG preview: {e})")
        return None


async def complete_2fa_if_needed(client, password_fn=getpass.getpass) -> bool:
    """Prompts for Telegram Two-Step Verification password when required."""
    for attempt in range(3):
        pwd = password_fn("Two-Step Verification enabled. Enter your Telegram 2FA password: ")
        try:
            await client.sign_in(password=pwd)
            return True
        except errors.PasswordHashInvalidError:
            print(f"Incorrect 2FA password ({attempt + 1}/3). Please try again.")
    print("Too many invalid 2FA attempts.")
    return False


async def login_with_qr(client, password_fn=getpass.getpass, max_refreshes: int = 5) -> bool:
    """Authenticates via Telethon QR login so no SMS/OTP delivery is needed."""
    png_path = Path(tempfile.gettempdir()) / "creditlog_telegram_login_qr.png"
    qr = await client.qr_login()
    opened = False
    try:
        for refresh in range(max_refreshes):
            render_qr_code(qr.url, png_path, open_viewer=not opened)
            opened = True
            print(f"Waiting for scan in Telegram app (QR refreshes automatically in 28s, attempt {refresh + 1}/{max_refreshes})...")
            try:
                await qr.wait(timeout=28)
                return True
            except asyncio.TimeoutError:
                print("QR expired, generating a fresh code...")
                await qr.recreate()
            except errors.SessionPasswordNeededError:
                return await complete_2fa_if_needed(client, password_fn=password_fn)
    finally:
        if png_path.exists():
            try:
                png_path.unlink()
            except Exception:
                pass
    print("QR login timed out without being scanned.")
    return False


async def login_with_phone(
    client,
    phone: Optional[str] = None,
    input_fn=input,
    password_fn=getpass.getpass,
) -> Optional[bool]:
    """Phone + OTP login with explicit delivery diagnostics and ResendCode fallback.
    Returns True on success, False if user chooses to switch to QR, None on failure.
    """
    if not phone:
        phone = input_fn("Enter your Telegram phone number (international format, e.g. +2349035903917): ").strip()
    if not phone:
        print("Phone number is required.")
        return None
    if ":" in phone and phone.split(":", 1)[0].isdigit():
        print("Error: Bot tokens cannot be used here. Enter your personal Telegram user phone number.")
        return None

    sent = await client.send_code_request(phone)
    while True:
        print("\n[Telegram Code Delivery]")
        print(f"  Method: {describe_sent_code_type(getattr(sent, 'type', None))}")
        next_type = getattr(sent, "next_type", None)
        if next_type:
            print(f"  Resend option available: type 'r' to request code via {next_type.__class__.__name__}")
        print("  Tip: type 'q' to switch to QR Code login (no OTP needed).")

        code = input_fn("\nEnter the code you received (or 'r' to resend / 'q' for QR): ").strip()
        if code.lower() == "q":
            return False
        if code.lower() == "r":
            if not next_type:
                print("Telegram did not offer an alternate resend channel for this request. Switching to QR ('q') is recommended.")
                continue
            try:
                sent = await client(functions.auth.ResendCodeRequest(phone, sent.phone_code_hash))
                continue
            except Exception as e:
                print(f"Resend failed: {e}")
                continue
        if not code:
            continue

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            return True
        except errors.SessionPasswordNeededError:
            return await complete_2fa_if_needed(client, password_fn=password_fn)
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
            print(f"Login code error: {e}. Check the code, or type 'r' to resend / 'q' for QR.")


async def async_main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    print("=" * 60)
    print("TELEGRAM STRING SESSION GENERATOR")
    print("=" * 60)

    api_id_env = os.getenv("TELEGRAM_API_ID", "").strip().strip(' "\'')
    api_hash_env = os.getenv("TELEGRAM_API_HASH", "").strip().strip(' "\'')

    if not api_id_env:
        api_id_env = input("Enter your Telegram API_ID (from my.telegram.org): ").strip()
    if not api_hash_env:
        api_hash_env = input("Enter your Telegram API_HASH: ").strip()

    if not api_id_env or not api_hash_env:
        print("API_ID and API_HASH are required.")
        sys.exit(1)
    try:
        api_id = int(api_id_env)
    except ValueError:
        print(f"Invalid TELEGRAM_API_ID: '{api_id_env}' must be numeric.")
        sys.exit(1)

    print("\nChoose login method:")
    print("  [1] QR Code Scan (Recommended — no SMS/OTP code needed)")
    print("  [2] Phone Number & Code (shows delivery channel + SMS resend)")
    choice = input("\nSelect [1/2] (default 1): ").strip() or "1"

    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash_env,
        device_model="Desktop",
        system_version="Windows 11",
        app_version="5.9.0",
        lang_code="en",
        system_lang_code="en",
    )
    await client.connect()
    try:
        ok = False
        if choice == "2":
            res = await login_with_phone(client)
            if res is False:
                print("\nSwitching to QR Code login...")
                ok = await login_with_qr(client)
            else:
                ok = bool(res)
        else:
            ok = await login_with_qr(client)

        if not ok or not await client.is_user_authorized():
            print("\nLogin was not completed.")
            sys.exit(1)

        session_str = client.session.save()
        print("\n" + "=" * 60)
        print("LOGIN SUCCESSFUL!")
        print("=" * 60)
        print("\nYour TELEGRAM_STRING_SESSION string is:\n")
        print(session_str)
        print("\n" + "=" * 60)
        print("FLY.IO DEPLOYMENT INSTRUCTION:")
        print("Set this string session in Fly.io secrets so the bot runs in the cloud:")
        print(f'\nfly secrets set TELEGRAM_STRING_SESSION="{session_str}" --app creditlog-bot\n')
        print("IMPORTANT: Do NOT reuse this same session locally while Fly.io is running,")
        print("or Telegram will revoke it (AuthKeyDuplicatedError).")
        print("=" * 60)
    finally:
        await client.disconnect()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
