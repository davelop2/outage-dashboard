#!/usr/bin/env python3
"""
Checks each tool's status against its official source and:
  1) Writes data/status.json with the current snapshot (read by the dashboard).
  2) Compares it against the previous snapshot and, if anything moved to
     degraded/down, sends a card to Microsoft Teams (Workflows webhook).

Designed to run every N minutes via GitHub Actions (see
.github/workflows/monitor.yml), but works the same in a local cron job.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_FILE = os.path.join(ROOT, "services.json")
STATUS_FILE = os.path.join(ROOT, "data", "status.json")

TEAMS_WEBHOOK_URL = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
TIMEOUT = 10

# Normalized statuses used across the whole dashboard, best to worst
OK, DEGRADED, DOWN, UNKNOWN, MANUAL = "operational", "degraded", "down", "unknown", "manual"


def http_get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; outage-dashboard/1.0; +https://github.com)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_statuspage(service):
    """Services on Atlassian Statuspage.io: Asana, Jira/Atlassian, Zoom, DocuSign, etc."""
    try:
        data = http_get_json(service["api_url"])
        indicator = data.get("status", {}).get("indicator", "unknown")
        description = data.get("status", {}).get("description", "")
        mapping = {
            "none": OK,
            "minor": DEGRADED,
            "major": DOWN,
            "critical": DOWN,
        }
        return mapping.get(indicator, UNKNOWN), description or indicator
    except Exception as e:
        return UNKNOWN, f"Could not reach the status page ({e})"


def check_statuspage_best_effort(service):
    """Same as statuspage, but for a provider whose public endpoint we
    haven't confirmed is always available — if it fails, it's marked
    'review manually' instead of showing a false 'operational'."""
    status, desc = check_statuspage(service)
    if status == UNKNOWN:
        return UNKNOWN, f"Endpoint unavailable or changed. Review manually: {service['status_url']}"
    return status, desc


def check_salesforce_trust(service):
    """Public Salesforce Trust API (also covers products like Tableau via /products/)."""
    try:
        data = http_get_json(service["api_url"])
        # The API returns a list of instances with their status
        statuses = [row.get("status") for row in data if isinstance(row, dict)]
        if any(s == "MAJOR_INCIDENT" for s in statuses):
            return DOWN, "Major incident reported on Salesforce Trust"
        if any(s in ("MINOR_INCIDENT", "MAINTENANCE") for s in statuses):
            return DEGRADED, "Minor incident or maintenance in progress"
        if statuses:
            return OK, "Available"
        return UNKNOWN, "No instance data returned"
    except Exception as e:
        return UNKNOWN, f"Could not reach the Salesforce Trust API ({e})"


def check_manual(service):
    return MANUAL, service.get("note", "Requires manual review / no public API.")


def check_html_scrape(service):
    """For status pages that don't expose a JSON API and just render the
    status text directly in the HTML (e.g. Call Tracking Metrics, on Rootly
    rather than Statuspage.io). Looks for an 'operational' banner in the
    page text — approximate by nature, so treat it as best-effort."""
    try:
        req = urllib.request.Request(service["status_url"], headers={
            "User-Agent": "Mozilla/5.0 (compatible; outage-dashboard/1.0; +https://github.com)",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="ignore").lower()
        if "operational" in html:
            return OK, "Status page reports all systems operational"
        return DEGRADED, f"No 'operational' banner found on the page — check manually: {service['status_url']}"
    except Exception as e:
        return UNKNOWN, f"Could not reach the status page ({e})"


def check_adobe_status(service):
    """Adobe's internal (undocumented) status feed at
    data.status.adobe.com/adobestatus/StatusEvents. It returns the FULL
    incident history (hundreds of entries), not just active ones — an
    incident/product is still open when it has no 'endedOn' timestamp; when
    open, the most recent entry in its 'history' dict gives the current
    severity. This isn't an official public API like Statuspage, so treat
    it as best-effort: if Adobe changes this shape, it fails safe to
    UNKNOWN rather than a false 'operational'."""
    try:
        data = http_get_json(service["api_url"])
        incidents = data.get("incidentEvent", {}).get("incidents", {})
        sev_rank = {"Trivial": 1, "Minor": 1, "Major": 2, "Critical": 2}
        sev_status = {1: DEGRADED, 2: DOWN}
        worst_rank = 0
        open_items = []
        for inc in incidents.values():
            for prod in inc.get("products", {}).values():
                if prod.get("endedOn"):
                    continue  # resolved
                history = prod.get("history", {})
                if not history:
                    continue
                latest_key = max(history.keys(), key=lambda k: int(k))
                latest = history[latest_key]
                if latest.get("status") != "Opened":
                    continue
                rank = sev_rank.get(latest.get("severity", ""), 1)
                worst_rank = max(worst_rank, rank)
                open_items.append(f"{prod.get('name', 'Unknown')} ({latest.get('severity', 'unknown')})")
        if not open_items:
            return OK, "No open incidents reported"
        status = sev_status.get(worst_rank, DEGRADED)
        return status, "Open incident(s): " + ", ".join(open_items[:4])
    except Exception as e:
        return UNKNOWN, f"Could not reach Adobe's status feed ({e})"


def check_ms_status_post(service):
    """Public endpoint at status.cloud.microsoft (no login, no CORS issue since
    this runs server-side). Note: 'mac' only reports whether the M365 admin
    center itself is reachable, not the real per-tenant status of
    Exchange/Teams/SharePoint."""
    try:
        data = http_get_json(service["api_url"])
        status_text = (data.get("Status") or "").strip()
        message = (data.get("Message") or "")
        # Microsoft's message field can contain raw HTML (e.g. "<div>...").
        # Strip tags before truncating so we never inject a broken/unclosed
        # tag into the dashboard's innerHTML.
        message = re.sub(r"<[^>]+>", " ", message)
        message = re.sub(r"\s+", " ", message).strip()
        if status_text.lower() == "available":
            return OK, "M365 admin center available (does not reflect per-service/tenant incidents)"
        return DEGRADED, f"Reported status: {status_text or 'unknown'} — {message[:180]}"
    except Exception as e:
        return UNKNOWN, f"Could not reach status.cloud.microsoft ({e})"


CHECKERS = {
    "statuspage": check_statuspage,
    "statuspage_best_effort": check_statuspage_best_effort,
    "salesforce_trust": check_salesforce_trust,
    "salesforce_trust_product": check_salesforce_trust,  # simplified: same base endpoint
    "ms_status_post": check_ms_status_post,
    "html_scrape": check_html_scrape,
    "adobe_status": check_adobe_status,
    "manual": check_manual,
}
# Note: check_statuspage_best_effort stays available for the day you add a
# service whose public endpoint isn't 100% confirmed (see README).


# ---------------------------------------------------------------------------
# Microsoft 365 — real per-service status, via Microsoft Graph
# Only activates if the 3 environment variables MS_TENANT_ID, MS_CLIENT_ID,
# MS_CLIENT_SECRET exist (secrets for a read-only app registered in Entra ID
# — see README, "Full Microsoft 365 integration" section).
# If they're not set, the panel keeps showing the partial "mac" signal.
# ---------------------------------------------------------------------------

GRAPH_STATUS_MAP = {
    "serviceoperational": OK,
    "resolved": OK,
    "falsepositive": OK,
    "postincidentreviewpublished": OK,
    "investigationsuspended": OK,
    "investigating": DEGRADED,
    "verifyingservice": DEGRADED,
    "restoringservice": DEGRADED,
    "servicedegradation": DEGRADED,
    "extendedrecovery": DEGRADED,
    "serviceinterruption": DOWN,
}


def get_graph_token():
    tenant = os.environ["MS_TENANT_ID"]
    client_id = os.environ["MS_CLIENT_ID"]
    client_secret = os.environ["MS_CLIENT_SECRET"]
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def check_ms_graph_all_services():
    """Returns a dict {id: {name, status, detail}} — one entry per M365
    service the tenant is subscribed to (Exchange, Teams, SharePoint...)."""
    token = get_graph_token()
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/healthOverviews",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = {}
    for item in data.get("value", []):
        service_name = item.get("service", "Unknown service")
        raw_status = (item.get("status") or "").lower()
        status = GRAPH_STATUS_MAP.get(raw_status, UNKNOWN)
        service_id = "m365_" + service_name.lower().replace(" ", "_").replace("/", "_")
        results[service_id] = {
            "name": f"Microsoft 365 — {service_name}",
            "status": status,
            "detail": f"Graph status: {item.get('status', 'unknown')}",
            "status_url": "https://admin.microsoft.com/adminportal/home#/servicehealth",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    return results


def ms_graph_credentials_available():
    return all(os.environ.get(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"))


def load_previous():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def send_teams_alert(changed):
    """Sends a MessageCard to a Teams Workflows webhook.
    IMPORTANT: since May 2026 the classic 'Incoming Webhook' connectors from
    Office 365 Connectors no longer work. You need a webhook created with the
    'Workflows' app in Teams (template: 'Send webhook alerts to a channel'),
    which still accepts this same MessageCard format.
    """
    if not TEAMS_WEBHOOK_URL:
        print("TEAMS_WEBHOOK_URL not set — skipping the Teams notification.")
        return

    facts = [{"name": c["name"], "value": f"{c['from']} → {c['to']}"} for c in changed]
    worst = "down" if any(c["to"] == DOWN for c in changed) else "degraded"
    color = "B3311F" if worst == "down" else "C77D18"
    title = "🔴 Outage detected" if worst == "down" else "🟡 Degradation detected"

    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": color,
        "summary": title,
        "sections": [{
            "activityTitle": title,
            "activitySubtitle": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "facts": facts,
            "markdown": True,
        }],
    }

    req = urllib.request.Request(
        TEAMS_WEBHOOK_URL,
        data=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            print(f"Alert sent to Teams (HTTP {resp.status}).")
    except urllib.error.HTTPError as e:
        print(f"Error sending to Teams: {e.code} {e.read()[:300]}")


def main():
    with open(SERVICES_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)["services"]

    previous = load_previous().get("services", {})
    now = datetime.now(timezone.utc).isoformat()

    results = {}
    changed = []

    for service in config:
        # Microsoft 365: if Graph credentials exist, swap the generic check
        # for the real per-service detail for the tenant.
        if service["id"] == "microsoft365" and ms_graph_credentials_available():
            try:
                graph_results = check_ms_graph_all_services()
                for sid, row in graph_results.items():
                    results[sid] = row
                    prev_status = previous.get(sid, {}).get("status")
                    if prev_status and prev_status != row["status"] and row["status"] in (DEGRADED, DOWN):
                        changed.append({"name": row["name"], "from": prev_status, "to": row["status"]})
                continue
            except Exception as e:
                print(f"Graph call failed, falling back to the public M365 signal ({e})")
                # falls through to the normal check ('mac' endpoint) as a fallback

        checker = CHECKERS.get(service["type"], check_manual)
        status, detail = checker(service)
        results[service["id"]] = {
            "name": service["name"],
            "status": status,
            "detail": detail,
            "status_url": service["status_url"],
            "checked_at": now,
        }

        prev_status = previous.get(service["id"], {}).get("status")
        # Only alert when it worsens to degraded/down (avoids noise from 'manual'/'unknown')
        if prev_status and prev_status != status and status in (DEGRADED, DOWN):
            changed.append({"name": service["name"], "from": prev_status, "to": status})

    # Manual test trigger (workflow_dispatch checkbox) — sends a real Teams
    # alert through the exact same code path as a real incident, without
    # touching the actual status data.
    if os.environ.get("TEST_ALERT", "").lower() in ("true", "1"):
        changed.append({"name": "Test alert (manually triggered)", "from": "operational", "to": "down"})

    snapshot = {"generated_at": now, "services": results}

    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    if changed:
        send_teams_alert(changed)
    else:
        print("No changes to alert on.")

    print(json.dumps({k: v["status"] for k, v in results.items()}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
