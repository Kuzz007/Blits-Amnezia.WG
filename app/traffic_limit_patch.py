import datetime
import re

from fastapi import HTTPException
from fastapi.responses import RedirectResponse


def _clean_client_name(value: str) -> str:
    value = re.sub(r"[\r\n]", " ", value or "").strip()
    value = re.sub(r"[\\'\"`;|&<>$]", "", value)
    return value or "client"


def _parse_datetime_local(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("expiration date is required")
    return datetime.datetime.fromisoformat(value).isoformat()


def _limit_bytes(limit_gb: float) -> int:
    return int(limit_gb * 1024 * 1024 * 1024) if limit_gb > 0 else 0


def _ensure_traffic_counter_columns(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
    if "traffic_last_raw_bytes" not in columns:
        conn.execute("ALTER TABLE clients ADD COLUMN traffic_last_raw_bytes INTEGER DEFAULT 0")


def _account_usage(saved_total: int, last_raw: int, live_total: int) -> tuple[int, int]:
    if live_total <= 0:
        return saved_total, last_raw
    if last_raw <= 0:
        return saved_total, live_total
    if live_total >= last_raw:
        return saved_total + (live_total - last_raw), live_total
    return saved_total + live_total, live_total


def patch_refresh_client_traffic_usage(web_routes_module) -> None:
    _patch_client_edit_route(web_routes_module)

    original_refresh = getattr(web_routes_module, "refresh_client_traffic_usage", None)
    if original_refresh is None or getattr(original_refresh, "_saved_traffic_limit_patch", False):
        return

    def patched_refresh_client_traffic_usage(enforce_limits: bool = True):
        usage = web_routes_module.get_clients_traffic_usage()
        now = datetime.datetime.utcnow().isoformat()
        changed_limits = False

        conn = web_routes_module.get_db_connection()
        try:
            _ensure_traffic_counter_columns(conn)
            rows = conn.execute(
                """
                SELECT id, name, public_key, traffic_limit_gb, traffic_used_bytes, traffic_last_raw_bytes, disabled_at
                FROM clients
                WHERE deleted_at IS NULL
                """
            ).fetchall()

            for row in rows:
                live_total = int(usage.get(row["public_key"], {}).get("total", 0) or 0)
                saved_total = int(row["traffic_used_bytes"] or 0)
                last_raw = int(row["traffic_last_raw_bytes"] or 0)
                used_total, next_raw = _account_usage(saved_total, last_raw, live_total)

                if used_total != saved_total or next_raw != last_raw:
                    conn.execute(
                        "UPDATE clients SET traffic_used_bytes = ?, traffic_last_raw_bytes = ? WHERE id = ?",
                        (used_total, next_raw, row["id"]),
                    )

                limit_gb = float(row["traffic_limit_gb"] or 0)
                if not enforce_limits or limit_gb <= 0 or row["disabled_at"]:
                    continue

                limit_bytes = _limit_bytes(limit_gb)
                if used_total >= limit_bytes:
                    conn.execute("UPDATE clients SET disabled_at = ? WHERE id = ?", (now, row["id"]))
                    changed_limits = True
                    try:
                        web_routes_module.log_event(
                            "traffic_limit",
                            f"Клиент {row['name']} отключен: лимит трафика исчерпан.",
                            client_id=row["id"],
                            client_name=row["name"],
                            notify=True,
                        )
                    except Exception:
                        pass

            conn.commit()
        finally:
            conn.close()

        if changed_limits:
            web_routes_module.rebuild_and_sync_vpn_config()

    patched_refresh_client_traffic_usage._saved_traffic_limit_patch = True
    setattr(web_routes_module, "refresh_client_traffic_usage", patched_refresh_client_traffic_usage)


def _patch_client_edit_route(web_routes_module) -> None:
    try:
        import app.client_edit as client_edit_module
    except Exception:
        return

    router = getattr(client_edit_module, "router", None)
    if router is None or getattr(router, "_limit_aware_edit_patch", False):
        return

    async def limit_aware_edit_client_action(client_id: str, name: str, expires_at: str, traffic_limit_gb: float = 0.0, user: dict | None = None):
        clean_name = _clean_client_name(name)
        traffic_limit_gb = max(0.0, min(float(traffic_limit_gb), 1_000_000.0))

        try:
            normalized_expires_at = _parse_datetime_local(expires_at)
        except ValueError:
            raise HTTPException(status_code=400, detail="Некорректная дата окончания")

        live_usage = web_routes_module.get_clients_traffic_usage()
        conn = web_routes_module.get_db_connection()
        reenabled_by_limit = False
        disabled_by_new_limit = False
        try:
            _ensure_traffic_counter_columns(conn)
            client = conn.execute(
                "SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL",
                (client_id,),
            ).fetchone()
            if not client:
                raise HTTPException(status_code=404, detail="Клиент не найден")

            route_type = client["route_type"] if "route_type" in client.keys() else "local"
            saved_total = int(client["traffic_used_bytes"] or 0)
            last_raw = int(client["traffic_last_raw_bytes"] or 0)
            live_total = int(live_usage.get(client["public_key"], {}).get("total", 0) or 0)
            used_total, next_raw = _account_usage(saved_total, last_raw, live_total)
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
                    SET name = ?, expires_at = ?, traffic_limit_gb = ?, disabled_at = NULL, traffic_used_bytes = ?, traffic_last_raw_bytes = ?
                    WHERE id = ?
                    """,
                    (clean_name, normalized_expires_at, traffic_limit_gb, used_total, next_raw, client_id),
                )
                reenabled_by_limit = True
            elif should_disable:
                now = datetime.datetime.utcnow().isoformat()
                conn.execute(
                    """
                    UPDATE clients
                    SET name = ?, expires_at = ?, traffic_limit_gb = ?, disabled_at = ?, traffic_used_bytes = ?, traffic_last_raw_bytes = ?
                    WHERE id = ?
                    """,
                    (clean_name, normalized_expires_at, traffic_limit_gb, now, used_total, next_raw, client_id),
                )
                disabled_by_new_limit = True
            else:
                conn.execute(
                    """
                    UPDATE clients
                    SET name = ?, expires_at = ?, traffic_limit_gb = ?, traffic_used_bytes = ?, traffic_last_raw_bytes = ?
                    WHERE id = ?
                    """,
                    (clean_name, normalized_expires_at, traffic_limit_gb, used_total, next_raw, client_id),
                )

            conn.commit()
        finally:
            conn.close()

        if route_type != "cascade":
            web_routes_module.rebuild_and_sync_vpn_config()

        if reenabled_by_limit:
            web_routes_module.log_event("client_reenabled", f"Клиент {clean_name} включен: новый лимит трафика позволяет продолжить работу.", client_id, clean_name, notify=True)
        elif disabled_by_new_limit:
            web_routes_module.log_event("traffic_limit", f"Клиент {clean_name} отключен: новый лимит трафика уже исчерпан.", client_id, clean_name, notify=True)
        else:
            web_routes_module.log_event("client_edited", f"Клиент {clean_name} отредактирован.", client_id, clean_name, notify=True)

        return RedirectResponse(url="/clients", status_code=303)

    for route in getattr(router, "routes", []):
        if getattr(route, "path", "") == "/clients/{client_id}/edit" and "POST" in getattr(route, "methods", set()):
            route.endpoint = limit_aware_edit_client_action
            if hasattr(route, "dependant"):
                route.dependant.call = limit_aware_edit_client_action

    router._limit_aware_edit_patch = True
