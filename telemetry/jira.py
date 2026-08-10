import logging
import os

import requests


logger = logging.getLogger("WIS2_Jira")


def is_configured():
    return all(
        os.environ.get(key)
        for key in (
            "JIRA_URL",
            "JIRA_PROJECT_KEY",
            "JIRA_API_TOKEN",
        )
    )


def _build_headers():
    return {
        "Authorization": f"Bearer {os.environ['JIRA_API_TOKEN']}",
        "Content-Type": "application/json",
    }


def build_summary(alert):
    display = (alert.display_title or "").strip()
    title = (alert.title or "").strip()

    if display and title and display != title:
        return f"{display} | {title}"

    return display or title or "Untitled Alert"


def create_jira_ticket(summary, description):
    """POST a new issue to Jira. Returns (key, error)."""

    if not is_configured():
        return None, (
            "Jira is not configured "
            "(set JIRA_URL, JIRA_PROJECT_KEY, JIRA_API_TOKEN in .env)"
        )

    url = f"{os.environ['JIRA_URL'].rstrip('/')}/rest/api/2/issue"

    payload = {
        "fields": {
            "project": {
                "key": os.environ["JIRA_PROJECT_KEY"],
            },
            "issuetype": {
                "name": os.environ.get("JIRA_ISSUE_TYPE", "Incident"),
            },
            "summary": summary,
            "description": description,
        }
    }

    try:
        response = requests.post(
            url,
            headers=_build_headers(),
            json=payload,
            timeout=15,
            verify=True,
        )
    except requests.RequestException as e:
        logger.warning("Jira request failed: %s", e)
        return None, str(e)

    if response.status_code == 201:
        try:
            key = response.json().get("key")
        except ValueError:
            key = None

        return key, None

    logger.warning(
        "Jira returned %s: %s",
        response.status_code,
        response.text,
    )

    return None, response.text