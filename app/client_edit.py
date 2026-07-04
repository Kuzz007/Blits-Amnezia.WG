import datetime
import html
import re
from typing import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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


@router.get("/clients/{client_id}/edit", response_class=HTMLResponse)
async def edit_client_page(
    request: Request,
    client_id: str,
    user: dict = Depends(check_password_change_required),
):
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL", (client_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    client = dict(row)
    name = _escape(client.get("name"))
    expires_at = _escape(_format_datetime_local(client.get("expires_at") or ""))
    traffic_limit_gb = _escape(client.get("traffic_limit_gb") or 0)
    route_type = _escape(client.get("route_type") or "local")
    client_ip = _escape(client.get("remote_ip_address") or client.get("ip_address") or "")
    theme = _escape(request.cookies.get("panel_theme", "light"))
    safe_client_id = _escape(client_id)

    disabled_note = (
        "<p class='hint warning'>Клиент сейчас отключен. Редактирование не включает его автоматически.</p>"
        if client.get("disabled_at")
        else ""
    )
    cascade_note = (
        "<p class='hint warning'>Это каскадный клиент. Изменения сохраняются в локальной панели; удаленная панель может требовать отдельной синхронизации.</p>"
        if route_type == "cascade"
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
    return RedirectResponse(url=f"/clients/{client_id}", status_code=303)


def _inject_client_edit_buttons(html_text: str) -> str:
    """
    Add an Edit button to every client row on the clients list page.
    """
    if "Редактировать клиента" in html_text:
        return html_text

    pattern = re.compile(
        r"(<button class=\"btn btn-primary btn-sm\" onclick=\"openConfigModal\('([^']+)'\)\" title=\"Конфигурация ключа\">\s*"
        r"<i class=\"fa-solid fa-key\"></i> Конфиг\s*</button>)"
    )

    def repl(match: re.Match) -> str:
        client_id = html.escape(match.group(2), quote=True)
        edit_button = (
            f'<a href="/clients/{client_id}/edit" '
            'class="btn btn-secondary btn-sm btn-icon-only" '
            'title="Редактировать клиента">'
            '<i class="fa-solid fa-pen-to-square"></i>'
            '</a>'
        )
        return edit_button + "\n                                                " + match.group(1)

    return pattern.sub(repl, html_text)


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

        html_text = _inject_client_edit_buttons(html_text)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return HTMLResponse(content=html_text, status_code=response.status_code, headers=headers)
