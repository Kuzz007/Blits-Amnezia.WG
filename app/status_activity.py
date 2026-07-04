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
