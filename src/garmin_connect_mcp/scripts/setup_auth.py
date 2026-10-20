"""Interactive authentication setup script for Garmin Connect MCP.

The background server never completes a login that needs MFA, so this is the one
place a fresh token store gets created: credentials are saved to .env, then a full
login runs here with the MFA code typed at the terminal, and garminconnect writes
garmin_tokens.json into the token store.
"""

import sys
from pathlib import Path

from dotenv import set_key
from garminconnect import Garmin

from ..auth import get_token_store, load_config, validate_credentials


def main():
    """Run the interactive authentication setup."""
    print("=" * 60)
    print("Garmin Connect MCP - Authentication Setup")
    print("=" * 60)
    print()

    config = load_config()
    if validate_credentials(config) and input(
        f"Use saved credentials for {config.garmin_email}? [Y/n] "
    ).strip().lower() in ("", "y", "yes"):
        email, password = config.garmin_email, config.garmin_password
    else:
        print("Enter your Garmin Connect credentials:")
        print("-" * 60)
        email = input("Email: ").strip()
        password = input("Password: ").strip()

        if not email or not password:
            print("\nError: Email and password are required.")
            return

        env_path = Path.cwd() / ".env"
        if not env_path.exists():
            env_path.touch()
        set_key(str(env_path), "GARMIN_EMAIL", email)
        set_key(str(env_path), "GARMIN_PASSWORD", password)
        print("\n✓ Credentials saved to .env")

    tokenstore = get_token_store()
    print(f"\nLogging in to Garmin Connect (tokens go to {tokenstore})...")
    garmin = Garmin(email, password, prompt_mfa=lambda: input("MFA code: ").strip())
    try:
        garmin.login(tokenstore)
    except Exception as e:
        print(f"\nLogin failed: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print("=" * 60)
    print(f"✓ Logged in. Tokens saved to {tokenstore}/garmin_tokens.json")
    print("=" * 60)
    print()
    print("Reconnect the MCP server (/mcp → garmin → reconnect) to pick them up.")


if __name__ == "__main__":
    main()
