import datetime


def patch_refresh_client_traffic_usage(web_routes_module) -> None:
    original_refresh = getattr(web_routes_module, "refresh_client_traffic_usage", None)
    if original_refresh is None or getattr(original_refresh, "_saved_traffic_limit_patch", False):
        return

    def patched_refresh_client_traffic_usage(enforce_limits: bool = True):
        usage = web_routes_module.get_clients_traffic_usage()
        now = datetime.datetime.utcnow().isoformat()
        changed_limits = False

        conn = web_routes_module.get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, name, public_key, traffic_limit_gb, traffic_used_bytes, disabled_at
                FROM clients
                WHERE deleted_at IS NULL
                """
            ).fetchall()

            for row in rows:
                live_total = int(usage.get(row["public_key"], {}).get("total", 0) or 0)
                saved_total = int(row["traffic_used_bytes"] or 0)
                used_total = max(saved_total, live_total)

                if used_total > saved_total:
                    conn.execute(
                        "UPDATE clients SET traffic_used_bytes = ? WHERE id = ?",
                        (used_total, row["id"]),
                    )

                limit_gb = float(row["traffic_limit_gb"] or 0)
                if not enforce_limits or limit_gb <= 0 or row["disabled_at"]:
                    continue

                limit_bytes = int(limit_gb * 1024 * 1024 * 1024)
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
