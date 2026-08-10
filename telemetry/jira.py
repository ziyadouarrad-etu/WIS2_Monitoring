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


GISC_TO_ASSIGNEE = {
    "GISC-Toulouse": {
        "username": "dd1cd716-f84c-49de-bda5-8abd4a2fcdb4",
        "email": "transmet@meteo.fr"
    },
    "GISC-Beijing": {
        "username": "8313d1bc-53bb-40fa-b45c-6d1e3bc2207c",
        "email": "gisc-beijing-ims@cma.gov.cn"
    },
    "GISC-Casablanca": {
        "username": "8d8b5632-5085-41fe-b73a-dfca5f979e5f",
        "email": "gisc-casablanca@marocmeteo.ma"
    },
    "GISC-Pretoria": {
        "username": "7b154e88-0b5d-4556-ba57-37dc69bebec8",
        "email": "gisc-support@weathersa.co.za"
    },
    "GISC-Tokyo": {
        "username": "1405a968-5a54-4cc4-9b53-e65156bdec2a",
        "email": "wis-jma@met.kishou.go.jp"
    },
    "GISC-Exeter": {
        "username": "c8ace557-1959-498e-9c86-bab6658f086a",
        "email": "nim@metoffice.gov.uk"
    },
    "GISC-Seoul": {
        "username": "370fe147-b6b4-430e-a378-d66db0c697e5",
        "email": "gisc_op@korea.kr"
    },
    "GISC-Melbourne": {
        "username": "36d3f1db-5b27-40ab-a94b-867fd63035c0",
        "email": "srcs_all@bom.gov.au"
    },
    "GISC-Offenbach": {
        "username": "a0eb4ca1-807a-4692-a843-9be3ed405e28",
        "email": "met.servicedesk@dwd.de"
    },
    "GISC-Brasília": {
        "username": "8a76f9e8-59b6-4a48-9a94-83db603d20ab",
        "email": "wis2.oper@inmet.gov.br"
    },
    "GISC-New Delhi": {
        "username": "81c27252-3a94-4929-b60f-b755f0d1e655",
        "email": "gisc.delhi@imd.gov.in"
    },
    "GISC-Washington": {
        "username": "91d812d6-09d4-4de7-afac-50054b5583e7",
        "email": "nws.gisc.washington.support@noaa.gov"
    },
    "GISC-Jeddah": {
        "username": "0d8650c1-b4ff-46cc-b691-6521fb17b3f1",
        "email": "wisop@ncm.gov.sa"
    },
    "GISC-Moscow": {
        "username": "1e98e1e8-26b8-46d0-a9d2-8329ddde0372",
        "email": "wisop@avia.mecom.ru"
    },
    "GISC-Tehran": {
        "username": "6b5ef055-94e1-45aa-8b53-7a8c833eba03",
        "email": "wis2operat@irimo.ir"
    },
    "ECCC-MSC Global Discovery Catalogue": {
        "username": "a7be2d99-a336-47ec-a92e-1bf21a76e5b3",
        "email": "ECWeather-Meteo@ec.gc.ca"
    },
    "MetOffice/NOAA Global Cache": {
        "username": "91d812d6-09d4-4de7-afac-50054b5583e7",
        "email": "nws.gisc.washington.support@noaa.gov"
    }
}


def priority_for_severity(severity):
    sev = (severity or "").upper()
    if sev == "CRITICAL":
        return "High"
    if sev == "ERROR":
        return "Medium"
    return "Low"


def create_jira_ticket(summary, description, assignee_username=None, priority=None):
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

    if assignee_username:
        payload["fields"]["assignee"] = {"name": assignee_username}
    if priority:
        payload["fields"]["priority"] = {"name": priority}

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