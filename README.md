# Tool status dashboard

Checks the official status of each company tool, publishes a
self-updating dashboard, and alerts Microsoft Teams whenever something
goes down or degrades.

## Why it's built this way (and the real limits of each piece)

A chat artifact (like the ones you see in this conversation) only runs
while you have that tab open — it can't watch anything 24/7 or notify you
if you close the chat. For this to be a dashboard that's *actually* always
running, it's split into three real, free pieces:

1. **`scripts/check_status.py`** — queries each tool's official source and
   saves the result to `data/status.json`.
2. **`.github/workflows/monitor.yml`** — runs that script every 10 minutes
   via GitHub Actions (free on normal repos), publishes `site/` to GitHub
   Pages, and fires the Teams alert if it detects a change to degraded/down.
3. **`site/index.html`** — the visual dashboard, which reads `status.json`
   and refreshes itself every 60 seconds.

## Real coverage per tool

Not every tool you mentioned exposes a public status without logging in.
Here's how each one ended up in `services.json`:

| Tool | Source | Automatable without login |
|---|---|---|
| Asana | status.asana.com (Statuspage) | Yes |
| Jira / Atlassian | status.atlassian.com (Statuspage) | Yes |
| Zoom | status.zoom.us (Statuspage) | Yes |
| DocuSign | status.docusign.com (Statuspage) | Yes |
| Call Tracking Metrics | status.calltrackingmetrics.com | Yes |
| Salesforce | Salesforce Trust API | Yes |
| Tableau Cloud | Salesforce Trust (Tableau product) | Yes |
| ServiceNow | status.servicenow.com | Best effort — can fail, falls back to "review manually" |
| **Microsoft 365** | `status.cloud.microsoft/api/posts/mac` | **Yes, but partial** — see note below |
| **AWS** | No single JSON with CORS; it's RSS per service/region | **Partial** — tell me which services/regions you use and I'll generate the exact feeds |

NetSuite, Verizon, and Foxit were removed from the panel on request — they
didn't have a reliable public source to automate against. If you want to
bring them back later, the simplest option is a manual check (a fixed link
in the dashboard) or, for Foxit/NetSuite, a simple HTTP ping to their URL
as an availability proxy.

For the "Best effort" ones, the script tries the standard public
Statuspage.io endpoint; if the provider changes or locks it down, the panel
marks it "manual review" instead of showing a false "operational".

### Microsoft 365 — what the connected endpoint covers, and what it doesn't
We found (by inspecting the browser's Network tab) a public, no-login
endpoint: `https://status.cloud.microsoft/api/posts/mac`. The script
already uses it. But there's a real limitation worth being clear about:

- **What it reports**: whether the Microsoft 365 admin center itself
  (where admins check Service Health) is reachable or not.
- **What it does NOT report**: whether Exchange, Teams, SharePoint, or any
  other specific service is down for your users. That's *per-tenant*
  information, only available via authenticated Microsoft Graph
  (`ServiceHealth.Read`).

In practice: this endpoint is a useful backup signal (if it fails,
something big is happening at Microsoft's level), but it doesn't replace
real monitoring of "is my Outlook/Teams actually down?".

### Full Microsoft 365 — real status per service (Exchange, Teams, SharePoint...)

The script is already set up for this — `scripts/check_status.py` detects
whether the credentials exist and automatically switches from the partial
endpoint ("mac") to the real per-service status via Microsoft Graph
(`admin/serviceAnnouncement/healthOverviews`). All that's left is for
someone with an admin role in the tenant to do this once (10-15 min):

1. **Go to Entra ID → App registrations → New registration.**
   - Name: something like `outage-dashboard-readonly`.
   - Account type: *Single tenant* (your organization only).
   - Redirect URI: leave it blank, not needed.
2. Copy the **Application (client) ID** and the **Directory (tenant) ID**
   from the app's overview screen.
3. Go to **Certificates & secrets → New client secret**, copy the value as
   soon as it's generated (you can't view it again afterward).
4. Go to **API permissions → Add a permission → Microsoft Graph →
   Application permissions**, search for `ServiceHealth`, and check
   `ServiceHealth.Read.All`. Add it.
5. Click **"Grant admin consent for [your organization]"** (this requires
   the Global Admin or Privileged Role Admin role — it's the only step that
   truly needs someone with that permission).
6. In the GitHub repo, under **Settings → Secrets and variables → Actions**,
   create three secrets:
   - `MS_TENANT_ID` → the Directory (tenant) ID from step 2.
   - `MS_CLIENT_ID` → the Application (client) ID from step 2.
   - `MS_CLIENT_SECRET` → the secret value from step 3.
7. Run the workflow manually once to confirm. If something fails, the
   script falls back to the public "mac" endpoint without breaking the
   panel — check the Action's log for the reason.

This app can only *read* service status (`ServiceHealth.Read.All`
permission, no access to mail, files, or anything else) — it can't modify
anything in the tenant.

Once connected, the dashboard stops showing a single "Microsoft 365" row
and instead shows one row per service in your subscription (Exchange
Online, Microsoft Teams, SharePoint Online, OneDrive for Business, etc.),
with the same color system and the same Teams alerts as everything else.

### AWS — how to close this out properly
Tell me which services (EC2, S3, RDS, etc.) and regions the company uses,
and I'll add the specific `health.aws.amazon.com` RSS feeds to the script.

## Setup (10 minutes)

1. Create a GitHub repo (can be private) and upload this whole folder.
2. Under **Settings → Pages**, choose "GitHub Actions" as the source.
3. In Teams, create a webhook using the **Workflows** app (not the classic
   "Incoming Webhook" — that stopped working in May 2026):
   channel → `⋯` → **Workflows** → template *"Send webhook alerts to a
   channel"* → copy the URL it gives you.
4. In the repo, go to **Settings → Secrets and variables → Actions** and
   create a secret named `TEAMS_WEBHOOK_URL` with that URL.
5. Go to the repo's **Actions** tab and run the workflow manually once
   ("Run workflow") to generate the first `status.json`.
6. Your dashboard is published at
   `https://<your-username>.github.io/<your-repo>/`.
7. (Optional, recommended) Follow the **"Full Microsoft 365"** section above
   to get real per-service status instead of the partial signal.

From there it runs on its own every 10 minutes, with nobody needing to
open anything, and notifies Teams as soon as something changes status.

## Editing what's monitored

Everything lives in `services.json`. To add or remove a tool, or adjust a
note, edit that file — nothing else needs to change.
