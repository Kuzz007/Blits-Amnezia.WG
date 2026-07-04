import datetime
import html
import re
from typing import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.audit import log_event
from app.database import get_db_connection
from app.routes import check_password_change_required
from app.vpn_manager import rebuild_and_sync_vpn_config

router = APIRouter(tags=["Client editing"])
ONLINE_TIMEOUT_SECONDS = 600
CLIENTS_AUTO_REFRESH_SECONDS = 1


def patch_disabled_clients_offline(web_routes_module) -> None:
    original_statuses = getattr(web_routes_module, "get_client_connection_statuses", None)
    if original_statuses is not None and not getattr(original_statuses, "_online_timeout_patch", False):
        def patched_statuses() -> dict:
            statuses = original_statuses()
            for status in statuses.values():
                seconds_ago = status.get("seconds_ago")
                status["online"] = seconds_ago is not None and seconds_ago <= ONLINE_TIMEOUT_SECONDS
            return statuses
        patched_statuses._online_timeout_patch = True
        setattr(web_routes_module, "get_client_connection_statuses", patched_statuses)

    original_client_view = getattr(web_routes_module, "_client_view", None)
    if original_client_view is None or getattr(original_client_view, "_disabled_offline_patch", False):
        return

    def patched_client_view(row: dict, connection_statuses: dict, traffic_usage: dict) -> dict:
        client = original_client_view(row, connection_statuses, traffic_usage)
        if client.get("disabled_at"):
            client["is_online"] = False
            client["last_seen_text"] = "отключен"
            client["connected_interface"] = ""
        return client

    patched_client_view._disabled_offline_patch = True
    setattr(web_routes_module, "_client_view", patched_client_view)


def _format_datetime_local(value: str) -> str:
    try:
        return datetime.datetime.fromisoformat(value).strftime("%Y-%m-%dT%H:%M")
    except Exception:
        return ""


def _parse_datetime_local(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("expiration date is required")
    return datetime.datetime.fromisoformat(value).isoformat()


def _clean_client_name(value: str) -> str:
    value = re.sub(r"[\r\n]", " ", value or "").strip()
    value = re.sub(r"[\\'\"`;|&<>$]", "", value)
    return value or "client"


def _load_client(client_id: str) -> dict:
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL", (client_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return dict(row)


def _client_payload(client: dict, client_id: str) -> dict:
    route_type = client.get("route_type") or "local"
    return {
        "id": client_id,
        "name": client.get("name") or "",
        "expires_at": _format_datetime_local(client.get("expires_at") or ""),
        "traffic_limit_gb": client.get("traffic_limit_gb") or 0,
        "route_type": route_type,
        "ip": client.get("remote_ip_address") or client.get("ip_address") or "",
        "disabled": bool(client.get("disabled_at")),
        "cascade": route_type == "cascade",
    }


def _client_status_payload(client: dict) -> dict:
    if client.get("traffic_limit_exceeded"):
        key_label, key_class = "Лимит исчерпан", "badge-danger"
    elif client.get("disabled_at"):
        key_label, key_class = "Выключен", "badge-gray"
    elif client.get("is_expired"):
        key_label, key_class = "Истек срок", "badge-danger"
    else:
        key_label, key_class = "Активен", "badge-success"

    return {
        "id": client.get("id"),
        "key_label": key_label,
        "key_class": key_class,
        "is_online": bool(client.get("is_online")),
        "last_seen_text": client.get("last_seen_text") or "еще не подключался",
        "connected_interface": client.get("connected_interface") or "",
        "traffic_used_text": client.get("traffic_used_text") or "0 B",
        "traffic_limit_text": client.get("traffic_limit_text") or "Безлимит",
        "traffic_percent": int(client.get("traffic_percent") or 0),
        "traffic_limit_exceeded": bool(client.get("traffic_limit_exceeded")),
    }


@router.get("/clients/statuses")
async def clients_statuses(request: Request, user: dict = Depends(check_password_change_required)):
    import app.routes as web_routes

    web_routes.enforce_expired_clients()
    web_routes.refresh_client_traffic_usage(enforce_limits=True)
    search = (request.query_params.get("search") or "").strip()
    conn = get_db_connection()
    try:
        if search:
            query = f"%{search}%"
            rows = conn.execute(
                "SELECT * FROM clients WHERE deleted_at IS NULL AND (name LIKE ? OR ip_address LIKE ?) ORDER BY created_at DESC",
                (query, query),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM clients WHERE deleted_at IS NULL ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()

    connection_statuses = web_routes.get_client_connection_statuses()
    traffic_usage = web_routes.get_clients_traffic_usage()
    clients = [
        _client_status_payload(web_routes._client_view(dict(row), connection_statuses, traffic_usage))
        for row in rows
    ]
    return JSONResponse({
        "online_timeout_seconds": ONLINE_TIMEOUT_SECONDS,
        "status_source": "latest_handshake",
        "active_probe": False,
        "clients": clients,
    })


@router.get("/clients/{client_id}/edit-data")
async def edit_client_data(client_id: str, user: dict = Depends(check_password_change_required)):
    return JSONResponse(_client_payload(_load_client(client_id), client_id))


@router.get("/clients/{client_id}/edit", response_class=HTMLResponse)
async def edit_client_page(client_id: str, user: dict = Depends(check_password_change_required)):
    return RedirectResponse(url="/clients", status_code=303)


@router.post("/clients/{client_id}/edit")
async def edit_client_action(
    client_id: str,
    name: str = Form(...),
    expires_at: str = Form(...),
    traffic_limit_gb: float = Form(0.0),
    user: dict = Depends(check_password_change_required),
):
    clean_name = _clean_client_name(name)
    traffic_limit_gb = max(0.0, min(float(traffic_limit_gb), 1_000_000.0))

    try:
        normalized_expires_at = _parse_datetime_local(expires_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата окончания")

    conn = get_db_connection()
    try:
        client = conn.execute("SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL", (client_id,)).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден")
        conn.execute(
            "UPDATE clients SET name = ?, expires_at = ?, traffic_limit_gb = ? WHERE id = ?",
            (clean_name, normalized_expires_at, traffic_limit_gb, client_id),
        )
        conn.commit()
        route_type = client["route_type"] if "route_type" in client.keys() else "local"
    finally:
        conn.close()

    if route_type != "cascade":
        rebuild_and_sync_vpn_config()

    log_event("client_edited", f"Клиент {clean_name} отредактирован.", client_id, clean_name, notify=True)
    return RedirectResponse(url="/clients", status_code=303)


def _edit_modal_markup() -> str:
    return f"""
<style>
.client-edit-meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:18px}}.client-edit-meta-item{{background:var(--gray-light);border:1px solid var(--border-color);border-radius:10px;padding:10px 12px;min-width:0}}.client-edit-meta-label{{display:block;color:var(--text-muted);font-size:11px;margin-bottom:4px}}.client-edit-meta-value{{display:block;color:var(--text-main);font-size:13px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.client-edit-alert{{display:none;margin:0 0 14px 0;padding:10px 12px;border-radius:10px;border:1px solid #fde68a;background:#fffbeb;color:#b45309;font-size:13px;line-height:1.4}}.client-edit-form-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
</style>
<div id="client-edit-modal" class="modal">
  <div class="modal-content" style="max-width:680px;">
    <div class="modal-header">
      <h3 class="modal-title"><i class="fa-solid fa-pen-to-square"></i> Редактировать клиента</h3>
      <button class="close-btn" onclick="closeClientEditModal()">&times;</button>
    </div>
    <form id="client-edit-form" method="post">
      <div class="modal-body">
        <div class="client-edit-meta">
          <div class="client-edit-meta-item"><span class="client-edit-meta-label">ID</span><span id="client-edit-id" class="client-edit-meta-value">—</span></div>
          <div class="client-edit-meta-item"><span class="client-edit-meta-label">Тип</span><span id="client-edit-route" class="client-edit-meta-value">—</span></div>
          <div class="client-edit-meta-item"><span class="client-edit-meta-label">IP</span><span id="client-edit-ip" class="client-edit-meta-value">—</span></div>
        </div>
        <p id="client-edit-disabled" class="client-edit-alert">Клиент сейчас отключен. Редактирование не включает его автоматически.</p>
        <p id="client-edit-cascade" class="client-edit-alert">Это каскадный клиент. Изменения сохраняются локально; удаленная панель может требовать отдельной синхронизации.</p>
        <div class="form-group"><label for="client-edit-name">Имя клиента</label><input id="client-edit-name" name="name" class="form-control" type="text" required></div>
        <div class="client-edit-form-grid">
          <div class="form-group"><label for="client-edit-expires">Действует до</label><input id="client-edit-expires" name="expires_at" class="form-control" type="datetime-local" required></div>
          <div class="form-group"><label for="client-edit-traffic">Лимит трафика, ГБ</label><input id="client-edit-traffic" name="traffic_limit_gb" class="form-control" type="number" min="0" step="0.1" required><span class="client-subtext">0 = безлимит. Ключи и IP клиента сохраняются.</span></div>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary btn-sm" onclick="closeClientEditModal()">Отмена</button>
        <button type="submit" class="btn btn-primary btn-sm"><i class="fa-solid fa-floppy-disk"></i> Сохранить</button>
      </div>
    </form>
  </div>
</div>
<script>
function escapeClientHtml(value){{return String(value??'').replace(/[&<>\"']/g,function(ch){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[ch];}});}}
async function openClientEditModal(clientId){{
  const modal=document.getElementById('client-edit-modal');
  const form=document.getElementById('client-edit-form');
  if(!modal||!form){{window.location.href='/clients';return;}}
  modal.classList.add('active');
  form.action='/clients/'+clientId+'/edit';
  document.getElementById('client-edit-id').textContent=clientId;
  document.getElementById('client-edit-name').value='';
  document.getElementById('client-edit-expires').value='';
  document.getElementById('client-edit-traffic').value='0';
  const response=await fetch('/clients/'+clientId+'/edit-data',{{credentials:'same-origin'}});
  if(!response.ok){{alert('Не удалось загрузить данные клиента.');return;}}
  const data=await response.json();
  document.getElementById('client-edit-id').textContent=data.id||clientId;
  document.getElementById('client-edit-route').textContent=data.route_type||'local';
  document.getElementById('client-edit-ip').textContent=data.ip||'—';
  document.getElementById('client-edit-name').value=data.name||'';
  document.getElementById('client-edit-expires').value=data.expires_at||'';
  document.getElementById('client-edit-traffic').value=data.traffic_limit_gb||0;
  document.getElementById('client-edit-disabled').style.display=data.disabled?'block':'none';
  document.getElementById('client-edit-cascade').style.display=data.cascade?'block':'none';
  document.getElementById('client-edit-name').focus();
}}
function closeClientEditModal(){{const modal=document.getElementById('client-edit-modal');if(modal)modal.classList.remove('active');}}
function findClientRow(clientId){{
  const checks=document.querySelectorAll('.client-check');
  for(const check of checks){{if(check.value===clientId)return check.closest('tr');}}
  return null;
}}
function renderTrafficCell(client){{
  const percent=Math.max(0,Math.min(100,Number(client.traffic_percent||0)));
  const bar=client.traffic_limit_text==='Безлимит'?'':'<div class="traffic-bar" title="'+percent+'%"><div class="traffic-fill '+(client.traffic_limit_exceeded?'danger':'')+'" style="--traffic-percent: '+percent+'%;"></div></div>';
  return '<div class="traffic-line"><strong style="color: var(--text-main);">'+escapeClientHtml(client.traffic_used_text)+'</strong><span>'+escapeClientHtml(client.traffic_limit_text)+'</span></div>'+bar;
}}
function renderKeyCell(client){{return '<span class="badge '+escapeClientHtml(client.key_class)+'">'+escapeClientHtml(client.key_label)+'</span>';}}
function renderConnectionCell(client){{
  const badge=client.is_online?'badge-success':'badge-gray';
  const dot=client.is_online?'online':'offline';
  const label=client.is_online?'Онлайн':'Давно не виделся';
  const details=escapeClientHtml(client.last_seen_text||'')+(client.connected_interface?', '+escapeClientHtml(client.connected_interface):'');
  return '<div class="status-cell"><span class="badge '+badge+'"><span class="status-dot '+dot+'"></span> '+label+'</span><span class="client-subtext">'+details+'</span></div>';
}}
async function refreshClientStatusesQuietly(){{
  if(document.hidden) return;
  if(document.querySelector('.modal.active')) return;
  if(document.querySelector('.client-check:checked')) return;
  if(document.querySelector('input:focus,textarea:focus,select:focus')) return;
  try{{
    const response=await fetch('/clients/statuses'+window.location.search,{{credentials:'same-origin',cache:'no-store'}});
    if(!response.ok) return;
    const data=await response.json();
    for(const client of data.clients||[]){{
      const row=findClientRow(client.id);
      if(!row||row.children.length<7) continue;
      row.children[4].innerHTML=renderTrafficCell(client);
      row.children[5].innerHTML=renderKeyCell(client);
      row.children[6].innerHTML=renderConnectionCell(client);
    }}
  }}catch(error){{}}
}}
setInterval(refreshClientStatusesQuietly, {CLIENTS_AUTO_REFRESH_SECONDS * 1000});
</script>
"""


def _inject_client_edit_ui(html_text: str) -> str:
    if "client-edit-modal" in html_text:
        return html_text

    pattern = re.compile(
        r"(<button class=\"btn btn-primary btn-sm\" onclick=\"openConfigModal\('([^']+)'\)\" title=\"Конфигурация ключа\">\s*"
        r"<i class=\"fa-solid fa-key\"></i> Конфиг\s*</button>)"
    )

    def repl(match: re.Match) -> str:
        client_id = html.escape(match.group(2), quote=True)
        edit_button = (
            f'<button type="button" class="btn btn-secondary btn-sm btn-icon-only" '
            f'onclick="openClientEditModal(\'{client_id}\')" title="Редактировать клиента">'
            '<i class="fa-solid fa-pen-to-square"></i></button>'
        )
        return match.group(1) + "\n                                                " + edit_button

    html_text = pattern.sub(repl, html_text)
    if "</body>" in html_text:
        html_text = html_text.replace("</body>", _edit_modal_markup() + "\n</body>", 1)
    return html_text


def install_client_edit_button_middleware(app) -> None:
    @app.middleware("http")
    async def add_client_edit_buttons(request: Request, call_next: Callable):
        response = await call_next(request)
        if request.url.path != "/clients" or response.status_code != 200:
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk

        try:
            html_text = body.decode("utf-8")
        except UnicodeDecodeError:
            return HTMLResponse(content=body, status_code=response.status_code, headers=dict(response.headers))

        html_text = _inject_client_edit_ui(html_text)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return HTMLResponse(content=html_text, status_code=response.status_code, headers=headers)
