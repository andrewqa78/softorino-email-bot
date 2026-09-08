"""Create a Gmail OAuth refresh token for local setup."""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


CREDENTIALS_FILE = Path(__file__).with_name("credentials.json")
TOKEN_FILE = Path(__file__).with_name("token.json")
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    if not CREDENTIALS_FILE.exists():
        raise SystemExit(
            f"Missing {CREDENTIALS_FILE.name}. Download the OAuth client JSON "
            "and place it in the project root."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
    )
    credentials = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )
    TOKEN_FILE.write_text(credentials.to_json(), encoding="utf-8")

    print(f"Saved OAuth token to {TOKEN_FILE}")
    print("Use the refresh_token value from token.json as GMAIL_REFRESH_TOKEN.")


if __name__ == "__main__":
    main()