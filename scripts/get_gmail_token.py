"""Generate Gmail API OAuth credentials for the WIS2 Monitoring app.

Run this ONCE on a machine with a browser (your local machine, not the server):

    pip install google-auth-oauthlib
    python scripts/get_gmail_token.py --client-secret client_secret.json

It opens a browser for the Google consent screen. After you approve, it prints
the GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN values to paste
into the server's .env. The refresh token is long-lived and stored server-side.
"""

import argparse
import json

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main():
    parser = argparse.ArgumentParser(description="Gmail API OAuth token generator")
    parser.add_argument(
        "--client-secret",
        required=True,
        help="Path to the OAuth client secret JSON downloaded from Google Cloud Console",
    )
    args = parser.parse_args()

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    print("\nAdd these to the server's .env file:\n")
    print(f"GMAIL_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print("\nOptional: GMAIL_SENDER_EMAIL=<the account you authorized (defaults to SMTP_USER)>")
    print("\nKeep these values secret. The refresh token is long-lived only if the "
          "Google Cloud OAuth app's publishing status is 'In production'; apps in "
          "'Testing' mode issue refresh tokens that expire after 7 days.")


if __name__ == "__main__":
    main()
