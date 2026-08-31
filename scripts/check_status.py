#!/usr/bin/env python3
"""
Chequea el estado de cada herramienta contra su fuente oficial y:
  1) Escribe data/status.json con el snapshot actual (lo lee el dashboard).
  2) Compara contra el snapshot anterior y, si algo pasó a estar caído /
     degradado, envía una tarjeta a Microsoft Teams (Workflows webhook).

Diseñado para correr cada N minutos vía GitHub Actions (ver
.github/workflows/monitor.yml), pero funciona igual en un cron local.
"""
import json
import os
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

# Estados normalizados que usa todo el panel, de mejor a peor
OK, DEGRADED, DOWN, UNKNOWN, MANUAL = "operational", "degraded", "down", "unknown", "manual"


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "outage-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_statuspage(service):
    """Servicios sobre Atlassian Statuspage.io: Asana, Jira/Atlassian, Zoom, DocuSign, etc."""
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
        return UNKNOWN, f"No se pudo consultar el status page ({e})"


def check_statuspage_best_effort(service):
    """Igual que statuspage, pero de un proveedor donde no confirmamos que el
    endpoint público exista siempre — si falla, se marca como 'revisar manualmente'
    en vez de mostrar un falso 'operational'."""
    status, desc = check_statuspage(service)
    if status == UNKNOWN:
        return UNKNOWN, f"Endpoint no disponible o cambió. Revisar manualmente: {service['status_url']}"
    return status, desc


def check_salesforce_trust(service):
    """API pública de Salesforce Trust (cubre también productos como Tableau vía /products/)."""
    try:
        data = http_get_json(service["api_url"])
        # La API devuelve una lista de instancias con su status
        statuses = [row.get("status") for row in data if isinstance(row, dict)]
        if any(s == "MAJOR_INCIDENT" for s in statuses):
            return DOWN, "Major incident reportado en Salesforce Trust"
        if any(s in ("MINOR_INCIDENT", "MAINTENANCE") for s in statuses):
            return DEGRADED, "Incidente menor o mantenimiento en curso"
        if statuses:
            return OK, "Available"
        return UNKNOWN, "Sin datos de instancias"
    except Exception as e:
        return UNKNOWN, f"No se pudo consultar Salesforce Trust API ({e})"


def check_manual(service):
    return MANUAL, service.get("note", "Requiere revisión manual / no hay API pública.")


def check_ms_status_post(service):
    """Endpoint público de status.cloud.microsoft (sin login, sin CORS porque
    esto corre server-side). Ojo: 'mac' solo indica si el admin center de M365
    está accesible, no el estado real de Exchange/Teams/SharePoint por tenant."""
    try:
        data = http_get_json(service["api_url"])
        status_text = (data.get("Status") or "").strip()
        message = (data.get("Message") or "")
        if status_text.lower() == "available":
            return OK, "Admin center de M365 disponible (no refleja incidentes por servicio/tenant)"
        return DEGRADED, f"Estado reportado: {status_text or 'desconocido'} — {message[:180]}"
    except Exception as e:
        return UNKNOWN, f"No se pudo consultar status.cloud.microsoft ({e})"


CHECKERS = {
    "statuspage": check_statuspage,
    "statuspage_best_effort": check_statuspage_best_effort,
    "salesforce_trust": check_salesforce_trust,
    "salesforce_trust_product": check_salesforce_trust,  # simplificado: mismo endpoint base
    "ms_status_post": check_ms_status_post,
    "manual": check_manual,
}


# ---------------------------------------------------------------------------
# Microsoft 365 — estado real por servicio, vía Microsoft Graph
# Solo se activa si existen las 3 variables de entorno MS_TENANT_ID,
# MS_CLIENT_ID, MS_CLIENT_SECRET (secretos de una app de solo lectura
# registrada en Entra ID — ver README, sección "Microsoft 365 completo").
# Si no están, el panel sigue mostrando la señal parcial de "mac".
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
    """Devuelve un dict {id: {name, status, detail}} — uno por cada servicio
    de M365 al que esté suscrito el tenant (Exchange, Teams, SharePoint...)."""
    token = get_graph_token()
    req = urllib.request.Request(
        "https://graph.microsoft.com/v1.0/admin/serviceAnnouncement/healthOverviews",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    results = {}
    for item in data.get("value", []):
        service_name = item.get("service", "Servicio desconocido")
        raw_status = (item.get("status") or "").lower()
        status = GRAPH_STATUS_MAP.get(raw_status, UNKNOWN)
        service_id = "m365_" + service_name.lower().replace(" ", "_").replace("/", "_")
        results[service_id] = {
            "name": f"Microsoft 365 — {service_name}",
            "status": status,
            "detail": f"Estado Graph: {item.get('status', 'unknown')}",
            "status_url": "https://admin.microsoft.com/adminportal/home#/servicehealth",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    return results


def ms_graph_credentials_available():
    return all(os.environ.get(k) for k in ("MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"))
# Nota: check_statuspage_best_effort queda disponible para el día que agreguen
# un servicio cuyo endpoint público no esté 100% confirmado (ver README).


def load_previous():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def send_teams_alert(changed):
    """Envía una MessageCard a un webhook de Teams Workflows.
    IMPORTANTE: desde mayo 2026 los webhooks 'Incoming Webhook' clásicos de
    Office 365 Connectors ya no funcionan. Hay que usar un webhook creado con
    la app 'Workflows' en Teams (plantilla: 'Post to a channel when a webhook
    request is received'), que sigue aceptando este mismo formato MessageCard.
    """
    if not TEAMS_WEBHOOK_URL:
        print("TEAMS_WEBHOOK_URL no configurado — se omite el envío a Teams.")
        return

    facts = [{"name": c["name"], "value": f"{c['from']} → {c['to']}"} for c in changed]
    worst = "down" if any(c["to"] == DOWN for c in changed) else "degraded"
    color = "B3311F" if worst == "down" else "C77D18"
    title = "🔴 Outage detectado" if worst == "down" else "🟡 Degradación detectada"

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
            print(f"Alerta enviada a Teams (HTTP {resp.status}).")
    except urllib.error.HTTPError as e:
        print(f"Error enviando a Teams: {e.code} {e.read()[:300]}")


def main():
    with open(SERVICES_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)["services"]

    previous = load_previous().get("services", {})
    now = datetime.now(timezone.utc).isoformat()

    results = {}
    changed = []

    for service in config:
        # Microsoft 365: si hay credenciales de Graph, reemplazamos el chequeo
        # genérico por el detalle real de cada servicio del tenant.
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
                print(f"Graph falló, uso el respaldo público de M365 ({e})")
                # sigue al chequeo normal (endpoint 'mac') como respaldo

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
        # Solo alertamos cuando empeora hacia degraded/down (evita ruido en 'manual'/'unknown')
        if prev_status and prev_status != status and status in (DEGRADED, DOWN):
            changed.append({"name": service["name"], "from": prev_status, "to": status})

    snapshot = {"generated_at": now, "services": results}

    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)

    if changed:
        send_teams_alert(changed)
    else:
        print("Sin cambios que alertar.")

    print(json.dumps({k: v["status"] for k, v in results.items()}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
