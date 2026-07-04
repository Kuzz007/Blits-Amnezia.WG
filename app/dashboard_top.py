def build_top_clients(web_routes_module, limit: int = 5) -> list[dict]:
    format_bytes = getattr(web_routes_module, "format_bytes")
    usage_by_peer = web_routes_module.get_clients_traffic_usage()
    statuses = web_routes_module.get_client_connection_statuses()

    conn = web_routes_module.get_db_connection()
    try:
        rows = conn.execute(
            "SELECT name, public_key, traffic_used_bytes FROM clients WHERE deleted_at IS NULL"
        ).fetchall()
    finally:
        conn.close()

    top_clients = []
    for row in rows:
        usage = usage_by_peer.get(row["public_key"], {})
        rx = int(usage.get("rx", 0) or 0)
        tx = int(usage.get("tx", 0) or 0)
        live_total = int(usage.get("total", rx + tx) or 0)
        saved_total = int(row["traffic_used_bytes"] or 0)
        total = max(live_total, saved_total)
        status = statuses.get(row["public_key"], {})
        top_clients.append({
            "name": row["name"],
            "online": bool(status.get("online")),
            "rx": format_bytes(rx),
            "tx": format_bytes(tx),
            "total": format_bytes(total),
            "total_bytes": total,
        })

    top_clients.sort(key=lambda item: item["total_bytes"], reverse=True)
    return top_clients[:limit]
