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


def patch_disabled_clients_offline(web_routes_module) -> None:
    """
    Make disabled clients display as offline even when awg still has a recent handshake.
    """
    original_client_view = getattr(web_routes_module, "_client_view", None)
    if original_client_view is None or getattr(original_client_view, "_disabled_offline_patch", False):
        return

    def _client_view_disabled_clients_offline(row: dict, connection_statuses: dict, traffic_usage: dict) -> dict:
        client = original_client_view(row, connection_statuses, traffic_usage)
        if client.get("disabled_at"):
            client["is_online"] = False
            client["last_seen_text"] = "отключен"
            client["connected_interface"] = ""
        return client

    _client_view_disabled_clients_offline._disabled_offline_patch = True
    setattr(web_routes_module, "_client_view", _client_view_disabled_clients_offline)


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


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _load_client(client_id: str):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL", (client_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    return dict(row)


def _client_edit_payload(client: dict, client_id: str) -> dict:
    return {
        "id": client_id,
        "name": client.get("name") or "",
        "expires_at": _format_datetime_local(client.get("expires_at") or ""),
        "traffic_limit_gb": client.get("traffic_limit_gb") or 0,
        "route_type": client.get("route_type") or "local",
        "ip": client.get("remote_ip_address") or client.get("ip_address") or "",
        "disabled": bool(client.get("disabled_at")),
        "cascade": (client.get("route_type") or "local") == "cascade",
    }


@router.get("/clients/{client_id}/edit-data")
async def edit_client_data(
    client_id: str,
    user: dict = Depends(check_password_change_required),
):
    client = _load_client(client_id)
    return JSONResponse(_client_edit_payload(client, client_id))


@router.get("/clients/{client_id}/edit", response_class=HTMLResponse)
async def edit_client_page(
    request: Request,
    client_id: str,
    user: dict = Depends(check_password_change_required),
):
    client = _load_client(client_id)
    data = _client_edit_payload(client, client_id)
    name = _escape(data["name"])
    expires_at = _escape(data["expires_at"])
    traffic_limit_gb = _escape(data["traffic_limit_gb"])
    route_type = _escape(data["route_type"])
    client_ip = _escape(data["ip"])
    theme = _escape(request.cookies.get("panel_theme", "light"))
    safe_client_id = _escape(client_id)

    disabled_note = (
        "<p class='hint warning'>Клиент сейчас отключен. Редактирование не включает его автоматически.</p>"
        if data["disabled"]
        else ""
    )
    cascade_note = (
        "<p class='hint warning'>Это каскадный клиент. Изменения сохраняются в локальной панели; удаленная панель может требовать отдельной синхронизации.</p>"
        if data["cascade"]
        else ""
    )

    return HTMLResponse(f"""
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Редактировать клиента | Blitz Panel</title>
    <link rel="stylesheet" href="/static/style.css">
    <style>
        body {{ padding: 32px; background: var(--body-bg, #f8fafc); color: var(--text-main, #111827); }}
        .edit-card {{ max-width: 720px; margin: 0 auto; background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e5e7eb); border-radius: 16px; padding: 28px; box-shadow: 0 10px 30px rgba(15, 23, 42, .08); }}
        .form-row {{ margin-bottom: 18px; }}
        label {{ display: block; margin-bottom: 8px; font-weight: 700; }}
        input {{ width: 100%; box-sizing: border-box; padding: 12px 14px; border: 1px solid var(--border-color, #d1d5db); border-radius: 10px; font-size: 15px; }}
        .actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 22px; }}
        .hint {{ color: var(--text-muted, #64748b); font-size: 14px; line-height: 1.45; }}
        .hint.warning {{ color: #b45309; background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px; padding: 10px 12px; }}
        .meta {{ margin-bottom: 22px; }}
    </style>
</head>
<body data-theme="{theme}">
    <div class="edit-card">
        <h1>Редактировать клиента</h1>
        <div class="meta hint">
            ID: <code>{safe_client_id}</code><br>
            Тип: <code>{route_type}</code><br>
            IP: <code>{client_ip}</code>
        </div>
        {disabled_note}
        {cascade_note}
        <form action="/clients/{safe_client_id}/edit" method="post">
            <div class="form-row">
                <label for="name">Имя клиента</label>
                <input id="name" name="name" type="text" value="{name}" required>
            </div>
            <div class="form-row">
                <label for="expires_at">Действует до</label>
                <input id="expires_at" name="expires_at" type="datetime-local" value="{expires_at}" required>
            </div>
            <div class="form-row">
                <label for="traffic_limit_gb">Лимит трафика, ГБ</label>
                <input id="traffic_limit_gb" name="traffic_limit_gb" type="number" min="0" step="0.1" value="{traffic_limit_gb}" required>
                <p class="hint">0 = безлимит. Ключи и IP клиента сохраняются.</p>
            </div>
            <div class="actions">
                <button class="btn btn-primary" type="submit">Сохранить</button>
                <a class="btn btn-secondary" href="/clients/{safe_client_id}">Назад к клиенту</a>
                <a class="btn btn-secondary" href="/clients">К списку клиентов</a>
            </div>
        </form>
    </div>
</body>
</html>
""")


@router.post("/clients/{client_id}/edit")
async def edit_client_action(
    client_id: str,
    name: str = Form(...),
    expires_at: str = Form(...),
    traffic_limit_gb: float = Form(0.0),
    user: dict = Depends(check_password_change_required),
):
    clean_name = _clean_client_name(name)
    if traffic_limit_gb < 0:
        traffic_limit_gb = 0.0
    if traffic_limit_gb > 1_000_000:
        traffic_limit_gb = 1_000_000.0

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

    log_event(
        "client_edited",
        f"Клиент {clean_name} отредактирован.",
        client_id,
        clean_name,
        notify=True,
    )
    return RedirectResponse(url="/clients", status_code=303)


def _edit_modal_markup() -> str:
    return """
<style>
    .client-edit-meta {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 10px;
        margin-bottom: 18px;
    }
    .client-edit-meta-item {
        background: var(--gray-light);
        border: 1px solid var(--border-color);
        border-radius: 10px;
        padding: 10px 12px;
        min-width: 0;
    }
    .client-edit-meta-label {
        display: block;
        color: var(--text-muted);
        font-size: 11px;
        margin-bottom: 4px;
    }
    .client-edit-meta-value {
        display: block;
        color: var(--text-main);
        font-size: 13px;
        font-weight: 700;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .client-edit-alert {
        display: none;
        margin: 0 0 14px 0;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid #fde68a;
        background: #fffbeb;
        color: #b45309;
        font-size: 13px;
        line-height: 1.4;
    }
    .client-edit-form-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 16px;
    }
</style>
<div id="client-edit-modal" class="modal">
    <div class="modal-content" style="max-width: 680px;">
        <div class="modal-header">
            <h3 class="modal-title"><i class="fa-solid fa-pen-to-square"></i> Редактировать клиента</h3>
            <button class="close-btn" onclick="closeClientEditModal()">&times;</button>
        </div>
        <form id="client-edit-form" method="post">
            <div class="modal-body">
                <div class="client-edit-meta">
                    <div class="client-edit-meta-item">
                        <span class="client-edit-meta-label">ID</span>
                        <span id="client-edit-id" class="client-edit-meta-value">—</span>
                    </div>
                    <div class="client-edit-meta-item">
                        <span class="client-edit-meta-label">Тип</span>
                        <span id="client-edit-route" class="client-edit-meta-value">—</span>
                    </div>
                    <div class="client-edit-meta-item">
                        <span class="client-edit-meta-label">IP</span>
                        <span id="client-edit-ip" class="client-edit-meta-value">—</span>
                    </div>
                </div>
                <p id="client-edit-disabled" class="client-edit-alert">Клиент сейчас отключен. Редактирование не включает его автоматически.</p>
                <p id="client-edit-cascade" class="client-edit-alert">Это каскадный клиент. Изменения сохраняются в локальной панели; удаленная панель может требовать отдельной синхронизации.</p>
                <div class="form-group">
                    <label for="client-edit-name">Имя клиента</label>
                    <input id="client-edit-name" name="name" class="form-control" type="text" required>
                </div>
                <div class="client-edit-form-grid">
                    <div class="form-group">
                        <label for="client-edit-expires">Действует до</label>
                        <input id="client-edit-expires" name="expires_at" class="form-control" type="datetime-local" required>
                    </div>
                    <div class="form-group">
                        <label for="client-edit-traffic">Лимит трафика, ГБ</label>
                        <input id="client-edit-traffic" name="traffic_limit_gb" class="form-control" type="number" min="0" step="0.1" required>
                        <span class="client-subtext">0 = безлимит. Ключи и IP клиента сохраняются.</span>
                    </div>
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
    async function openClientEditModal(clientId) {
        const modal = document.getElementById('client-edit-modal');
        const form = document.getElementById('client-edit-form');
        if (!modal || !form) {
            window.location.href = '/clients/' + clientId + '/edit';
            return;
        }
        modal.classList.add('active');
        form.action = '/clients/' + clientId + '/edit';
        document.getElementById('client-edit-id').textContent = clientId;
        document.getElementById('client-edit-name').value = '';
        document.getElementById('client-edit-expires').value = '';
        document.getElementById('client-edit-traffic').value = '0';
        try {
            const response = await fetch('/clients/' + clientId + '/edit-data', { credentials: 'same-origin' });
            if (!response.ok) throw new Error('edit data request failed');
            const data = await response.json();
            document.getElementById('client-edit-id').textContent = data.id || clientId;
            document.getElementById('client-edit-route').textContent = data.route_type || 'local';
            document.getElementById('client-edit-ip').textContent = data.ip || '—';
            document.getElementById('client-edit-name').value = data.name || '';
            document.getElementById('client-edit-expires').value = data.expires_at || '';
            document.getElementById('client-edit-traffic').value = data.traffic_limit_gb || 0;
            document.getElementById('client-edit-disabled').style.display = data.disabled ? 'block' : 'none';
            document.getElementById('client-edit-cascade').style.display = data.cascade ? 'block' : 'none';
            document.getElementById('client-edit-name').focus();
        } catch (error) {
            alert('Не удалось загрузить данные клиента. Открою обычную страницу редактирования.');
            window.location.href = '/clients/' + clientId + '/edit';
        }
    }

    function closeClientEditModal() {
        const modal = document.getElementById('client-edit-modal');
        if (modal) modal.classList.remove('active');
    }
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
            '<i class="fa-solid fa-pen-to-square"></i>'
            '</button>'
        )
        return edit_button + "\n                                                " + match.group(1)

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
