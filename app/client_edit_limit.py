import datetime
import re

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse

from app.audit import log_event
from app.database import get_db_connection
from app.routes import check_password_change_required
from app.vpn_manager import get_clients_traffic_usage, rebuild_and_sync_vpn_config

router = APIRouter(tags=["Client editing"])


def _parse_datetime_local(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("expiration date is required")
    return datetime.datetime.fromisoformat(value).isoformat()


def _clean_client_name(value: str) -> str:
    value = re.sub(r"[\r\n]", " ", value or "").strip()
    value = re.sub(r"[\\'\"`;|&<>$]", "", value)
    return value or "client"


def _limit_bytes(limit_gb: float) -> int:
    return int(limit_gb * 1024 * 1024 * 1024) if limit_gb > 0 else 0


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

    live_usage = get_clients_traffic_usage()
    conn = get_db_connection()
    reenabled_by_limit = False
    disabled_by_new_limit = False
    try:
        client = conn.execute(
            "SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL",
            (client_id,),
        ).fetchone()
        if not client:
            raise HTTPException(status_code=404, detail="Клиент не найден")

        route_type = client["route_type"] if "route_type" in client.keys() else "local"
        saved_total = int(client["traffic_used_bytes"] or 0)
        live_total = int(live_usage.get(client["public_key"], {}).get("total", 0) or 0)
        used_total = max(saved_total, live_total)
        old_limit_gb = float(client["traffic_limit_gb"] or 0)
        old_limit_bytes = _limit_bytes(old_limit_gb)
        new_limit_bytes = _limit_bytes(traffic_limit_gb)

        disabled_at = client["disabled_at"]
        was_disabled_by_old_limit = bool(disabled_at and old_limit_bytes and used_total >= old_limit_bytes)
        new_limit_allows_usage = new_limit_bytes == 0 or used_total < new_limit_bytes
        should_reenable = was_disabled_by_old_limit and new_limit_allows_usage
        should_disable = bool(not disabled_at and new_limit_bytes and used_total >= new_limit_bytes)

        if should_reenable:
            conn.execute(
                """
                UPDATE clients
                SET name = ?, expires_at = ?, traffic_limit_gb = ?, disabled_at = NULL, traffic_used_bytes = MAX(COALESCE(traffic_used_bytes, 0), ?)
                WHERE id = ?
                """,
                (clean_name, normalized_expires_at, traffic_limit_gb, used_total, client_id),
            )
            reenabled_by_limit = True
        elif should_disable:
            now = datetime.datetime.utcnow().isoformat()
            conn.execute(
                """
                UPDATE clients
                SET name = ?, expires_at = ?, traffic_limit_gb = ?, disabled_at = ?, traffic_used_bytes = MAX(COALESCE(traffic_used_bytes, 0), ?)
                WHERE id = ?
                """,
                (clean_name, normalized_expires_at, traffic_limit_gb, now, used_total, client_id),
            )
            disabled_by_new_limit = True
        else:
            conn.execute(
                """
                UPDATE clients
                SET name = ?, expires_at = ?, traffic_limit_gb = ?, traffic_used_bytes = MAX(COALESCE(traffic_used_bytes, 0), ?)
                WHERE id = ?
                """,
                (clean_name, normalized_expires_at, traffic_limit_gb, used_total, client_id),
            )

        conn.commit()
    finally:
        conn.close()

    if route_type != "cascade":
        rebuild_and_sync_vpn_config()

    if reenabled_by_limit:
        log_event("client_reenabled", f"Клиент {clean_name} включен: новый лимит трафика позволяет продолжить работу.", client_id, clean_name, notify=True)
    elif disabled_by_new_limit:
        log_event("traffic_limit", f"Клиент {clean_name} отключен: новый лимит трафика уже исчерпан.", client_id, clean_name, notify=True)
    else:
        log_event("client_edited", f"Клиент {clean_name} отредактирован.", client_id, clean_name, notify=True)

    return RedirectResponse(url="/clients", status_code=303)
