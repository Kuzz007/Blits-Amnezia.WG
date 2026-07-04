import time

TRAFFIC_ACTIVITY_GRACE_SECONDS = 45
_PEER_TRAFFIC_ACTIVITY: dict[str, dict[str, float | int]] = {}


def patch_statuses_by_traffic(web_routes_module) -> None:
    """Treat a peer as online when its WireGuard/AmneziaWG counters are moving.

    WireGuard handshakes may be older than the UI timeout even while real traffic is
    flowing through the peer. This keeps the handshake check as the default source,
    but upgrades the peer to online for a short grace window whenever rx/tx counters
    increase between status polls.
    """
    original_statuses = getattr(web_routes_module, "get_client_connection_statuses", None)
    if original_statuses is None or getattr(original_statuses, "_traffic_activity_patch", False):
        return

    def patched_statuses() -> dict:
        statuses = original_statuses()
        now = time.time()
        active_keys = set()

        for public_key, status in statuses.items():
            active_keys.add(public_key)
            total = int(status.get("transfer_rx", 0) or 0) + int(status.get("transfer_tx", 0) or 0)
            previous = _PEER_TRAFFIC_ACTIVITY.get(public_key)

            if previous is None:
                _PEER_TRAFFIC_ACTIVITY[public_key] = {
                    "total": total,
                    "changed_at": now if total > 0 and status.get("online") else 0,
                }
                continue

            if total > int(previous.get("total", 0)):
                previous["total"] = total
                previous["changed_at"] = now

            changed_at = float(previous.get("changed_at", 0) or 0)
            if changed_at and now - changed_at <= TRAFFIC_ACTIVITY_GRACE_SECONDS:
                status["online"] = True
                status["online_by_traffic"] = True

        for public_key in list(_PEER_TRAFFIC_ACTIVITY.keys()):
            if public_key not in active_keys:
                _PEER_TRAFFIC_ACTIVITY.pop(public_key, None)

        return statuses

    patched_statuses._traffic_activity_patch = True
    setattr(web_routes_module, "get_client_connection_statuses", patched_statuses)


def patch_dashboard_top_clients(web_routes_module) -> None:
    """Render dashboard top clients from summed live traffic usage.

    The original dashboard code builds a peer dict with update() across awg0 and
    awg_legacy. If the same public key exists on both interfaces, a later zero dump
    can overwrite live counters from the active interface. get_clients_traffic_usage()
    already sums rx/tx across interfaces, so use it for dashboard rows.
    """
    original_dashboard_stats = getattr(web_routes_module, "get_dashboard_stats", None)
    if original_dashboard_stats is None or getattr(original_dashboard_stats, "_top_clients_patch", False):
        return

    def patched_dashboard_stats() -> dict:
        stats = original_dashboard_stats()
        format_bytes = getattr(web_routes_module, "format_bytes")
        usage_by_peer = web_routes_module.get_clients_traffic_usage()
        statuses = web_routes_module.get_client_connection_statuses()

        top_clients = []
        conn = web_routes_module.get_db_connection()
        try:
            rows = conn.execute(
                "SELECT name, public_key, traffic_used_bytes FROM clients WHERE deleted_at IS NULL"
            ).fetchall()
        finally:
            conn.close()

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
        stats["top_clients"] = top_clients[:5]
        if "connections" in stats:
            stats["connections"]["online"] = sum(1 for item in statuses.values() if item.get("online"))
        return stats

    patched_dashboard_stats._top_clients_patch = True
    setattr(web_routes_module, "get_dashboard_stats", patched_dashboard_stats)
