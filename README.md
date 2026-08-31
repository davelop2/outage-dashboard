# Panel de estado de herramientas

Chequea el estado oficial de cada herramienta de la compañía, publica un
dashboard que se actualiza solo, y avisa en Microsoft Teams cuando algo
cae o se degrada.

## Por qué está armado así (y qué límite real tiene cada pieza)

Un artifact de chat (como los que ves en esta conversación) solo corre
mientras tienes esa pestaña abierta — no puede vigilar nada 24/7 ni avisarte
si cierras el chat. Para que esto sea un panel *de verdad* siempre activo,
lo dividí en tres piezas reales y gratuitas:

1. **`scripts/check_status.py`** — consulta la fuente oficial de cada
   herramienta y guarda el resultado en `data/status.json`.
2. **`.github/workflows/monitor.yml`** — ejecuta ese script cada 10 minutos
   en GitHub Actions (gratis en repos normales), publica `site/` en GitHub
   Pages, y dispara la alerta a Teams si detecta un cambio a degradado/caído.
3. **`site/index.html`** — el dashboard visual, que lee `status.json` y se
   refresca solo cada 60 segundos.

## Cobertura real por herramienta

No todas las herramientas que mencionaste exponen un estado público sin
iniciar sesión. Así quedó cada una en `services.json`:

| Herramienta | Fuente | Automatizable sin login |
|---|---|---|
| Asana | status.asana.com (Statuspage) | Sí |
| Jira / Atlassian | status.atlassian.com (Statuspage) | Sí |
| Zoom | status.zoom.us (Statuspage) | Sí |
| DocuSign | status.docusign.com (Statuspage) | Sí |
| Call Tracking Metrics | status.calltrackingmetrics.com | Sí |
| Salesforce | Salesforce Trust API | Sí |
| Tableau Cloud | Salesforce Trust (producto Tableau) | Sí |
| ServiceNow | status.servicenow.com | Best effort — puede fallar, se marca "revisar manualmente" |
| **Microsoft 365** | `status.cloud.microsoft/api/posts/mac` | **Sí, pero parcial** — ver nota abajo |
| **AWS** | Sin JSON único con CORS; es RSS por servicio/región | **Parcial** — dime qué servicios/regiones usan y te genero los feeds exactos |

NetSuite, Verizon y Foxit se quitaron del panel a pedido — no tenían una
fuente pública confiable para automatizar. Si más adelante quieren volver a
incluirlos, la forma más simple es sumarlos como chequeo manual (un link fijo
en el dashboard) o, en el caso de Foxit/NetSuite, un ping HTTP simple a su
URL como proxy de disponibilidad.

Para los "Best effort", el script intenta el endpoint público estándar de
Statuspage.io; si el proveedor lo cambia o lo protege, el panel lo marca
como "revisión manual" en vez de mentir con un falso "operativo".

### Microsoft 365 — qué sí y qué no cubre el endpoint conectado
Encontramos (revisando el Network tab del navegador) un endpoint público y
sin login: `https://status.cloud.microsoft/api/posts/mac`. El script ya lo
usa. Pero hay una limitación real que vale la pena tener clara:

- **Lo que reporta**: si el propio panel de administración de Microsoft 365
  (donde los admins revisan Service Health) está accesible o no.
- **Lo que NO reporta**: si Exchange, Teams, SharePoint u otro servicio
  específico está caído para tus usuarios. Eso es información *por tenant*
  y solo se obtiene con Microsoft Graph (`ServiceHealth.Read`), autenticado.

En la práctica: este endpoint es una señal de respaldo útil (si falla, algo
grande está pasando a nivel de Microsoft), pero no reemplaza un monitoreo
real de "¿mi Outlook/Teams está caído?".

### Microsoft 365 completo — estado real de cada servicio (Exchange, Teams, SharePoint...)

El script ya está listo para esto — `scripts/check_status.py` detecta si
existen las credenciales y automáticamente cambia del endpoint parcial
("mac") al estado real por servicio vía Microsoft Graph
(`admin/serviceAnnouncement/healthOverviews`). Solo falta que alguien con
rol de administrador en el tenant haga esto una vez (10-15 min):

1. **Ir a Entra ID → App registrations → New registration.**
   - Nombre: algo como `outage-dashboard-readonly`.
   - Tipo de cuenta: *Single tenant* (solo tu organización).
   - Redirect URI: déjalo vacío, no se necesita.
2. Copiar de la pantalla de la app el **Application (client) ID** y el
   **Directory (tenant) ID**.
3. Ir a **Certificates & secrets → New client secret**, copiar el valor
   apenas se genera (no se puede volver a ver después).
4. Ir a **API permissions → Add a permission → Microsoft Graph →
   Application permissions**, buscar `ServiceHealth` y marcar
   `ServiceHealth.Read.All`. Agregar.
5. Click en **"Grant admin consent for [tu organización]"** (esto requiere
   rol de Global Admin o Privileged Role Admin — es el único paso que de
   verdad necesita a alguien con ese permiso).
6. En el repo de GitHub, en **Settings → Secrets and variables → Actions**,
   crear tres secretos:
   - `MS_TENANT_ID` → el Directory (tenant) ID del paso 2.
   - `MS_CLIENT_ID` → el Application (client) ID del paso 2.
   - `MS_CLIENT_SECRET` → el valor del secreto del paso 3.
7. Corre el workflow manualmente una vez para confirmar. Si algo falla, el
   script cae de vuelta al endpoint público "mac" sin romper el panel —
   revisa el log de la Action para ver el motivo.

Esta app solo puede *leer* el estado de servicio (permiso `ServiceHealth.Read.All`,
sin acceso a correos, archivos, ni nada más) — no puede modificar nada en el
tenant.

Una vez conectado, el dashboard deja de mostrar una sola fila "Microsoft 365"
y en su lugar muestra una fila por cada servicio de tu suscripción (Exchange
Online, Microsoft Teams, SharePoint Online, OneDrive for Business, etc.),
con el mismo sistema de colores y las mismas alertas a Teams que el resto.

### AWS — cómo cerrarlo de verdad
Dime qué servicios (EC2, S3, RDS, etc.) y regiones usa la compañía y agrego
los feeds RSS específicos de `health.aws.amazon.com` al script.

## Instalación (10 minutos)

1. Crea un repo de GitHub (puede ser privado) y sube esta carpeta completa.
2. En **Settings → Pages**, elige "GitHub Actions" como fuente.
3. En Teams, crea un webhook con la app **Workflows** (no el "Incoming
   Webhook" clásico — ese ya no funciona desde mayo 2026):
   canal → `⋯` → **Workflows** → plantilla *"Post to a channel when a
   webhook request is received"* → copia la URL que te da.
4. En el repo, ve a **Settings → Secrets and variables → Actions** y crea
   un secreto llamado `TEAMS_WEBHOOK_URL` con esa URL.
5. Ve a la pestaña **Actions** del repo y corre el workflow manualmente una
   vez ("Run workflow") para generar el primer `status.json`.
6. Tu dashboard queda publicado en
   `https://<tu-usuario>.github.io/<tu-repo>/`.
7. (Opcional, recomendado) Sigue la sección **"Microsoft 365 completo"** más
   abajo para tener el estado real por servicio en vez de la señal parcial.

A partir de ahí corre solo cada 10 minutos, sin que nadie tenga que abrir
nada, y te avisa en Teams apenas algo cambie de estado.

## Editar qué se monitorea

Todo vive en `services.json`. Para agregar o quitar una herramienta, o
ajustar la nota de alguna, edita ese archivo — no hay que tocar el resto.
