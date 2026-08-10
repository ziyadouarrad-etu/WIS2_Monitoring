"""Find the Jira Data Center username for each WIS2 assignee.

Jira Server/Data Center requires an assignee sent as {"name": <username>}
(where username doubles as the DC accountId). This script queries the
read-only user search API for each assignee and reports which username
matches, so the mapping in telemetry/jira.py can use it.

Run from the project root with JIRA_URL / JIRA_API_TOKEN in .env (or env):

    python scripts/lookup_jira_usernames.py [--project-key WIS]

Optional --project-key also runs the "assignable users" search, which only
returns users that can actually be assigned in that project.
"""

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.jira import GISC_TO_ASSIGNEE

load_dotenv()


def build_headers():
    return {
        "Authorization": f"Bearer {os.environ['JIRA_API_TOKEN']}",
        "Content-Type": "application/json",
    }


def search(base_url, path, params, headers, retries=2):
    url = f"{base_url}/rest/api/2/{path}"
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30, verify=True)
            break
        except requests.RequestException as e:
            last_error = f"request error: {e}"
            time.sleep(1 + attempt * 2)
    else:
        return None, last_error
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text[:200]}"
    try:
        return response.json(), None
    except ValueError:
        return None, "Non-JSON response"


def summarize(user):
    return {
        "name": user.get("name") or user.get("key"),
        "accountId": user.get("accountId"),
        "emailAddress": user.get("emailAddress"),
        "displayName": user.get("displayName"),
        "active": user.get("active"),
    }


def main():
    parser = argparse.ArgumentParser(description="Find Jira usernames for GISC assignees")
    parser.add_argument("--project-key", help="Jira project key to check assignability (optional)")
    args = parser.parse_args()

    if not all(os.environ.get(k) for k in ("JIRA_URL", "JIRA_API_TOKEN")):
        print("JIRA_URL and JIRA_API_TOKEN must be set in .env or the environment.")
        return

    base_url = os.environ["JIRA_URL"].rstrip("/")
    headers = build_headers()
    report = {}

    for label, info in GISC_TO_ASSIGNEE.items():
        email = info.get("email", "").strip()
        print(f"\n=== {label} ===")
        if not email and not info.get("username"):
            print("  (no email and no username in mapping)")
            continue

        candidates = []
        seen = set()
        terms = []
        if email:
            terms.append(email)
        if info.get("username"):
            terms.append(info["username"])

        for term in terms:
            users, error = search(base_url, "user/search", {"username": term}, headers)
            if error:
                print(f"  user/search username={term}: {error}")
                continue
            for user in users or []:
                summary = summarize(user)
                key = summary["name"]
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(summary)

        if args.project_key:
            users, error = search(
                base_url,
                "user/assignable/search",
                {"project": args.project_key, "username": email or info.get("username", "")},
                headers,
            )
            if error:
                print(f"  assignable/search: {error}")
            else:
                for user in users or []:
                    summary = summarize(user)
                    key = summary["name"]
                    if key and key not in seen:
                        seen.add(key)
                        candidates.append(summary)

        if not candidates:
            print("  -> no users found")
            report[label] = {"email": email, "users": []}
            continue

        exact = [c for c in candidates if c["emailAddress"] and c["emailAddress"].lower() == email.lower()]
        bucket = exact or candidates
        print(f"  target email: {email}")
        for c in bucket:
            flag = "  <-- email matches" if c in exact else ""
            print(f"    username={c['name']} accountId={c['accountId']} "
                  f"email={c['emailAddress']} display={c['displayName']}{flag}")
        report[label] = {"email": email, "users": bucket}

    with open("jira_usernames_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print("\n\nReport written to jira_usernames_report.json")


if __name__ == "__main__":
    main()
