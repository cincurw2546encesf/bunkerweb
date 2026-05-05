from collections import Counter
from datetime import datetime
from logging import getLogger
from traceback import format_exc


def _normalize_service_name(value):
    if value is None:
        return ""
    normalized = str(value).strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    if lowered in {"all", "any"}:
        return ""
    if lowered in {"default", "default_server"}:
        return "_"
    return normalized.lower()


def _format_date(raw_value):
    if isinstance(raw_value, (int, float)):
        try:
            return datetime.fromtimestamp(raw_value).isoformat()
        except (OSError, ValueError):
            return datetime.fromtimestamp(0).isoformat()
    try:
        return datetime.fromtimestamp(float(raw_value)).isoformat()
    except (TypeError, ValueError, OSError):
        return str(raw_value or "")


def pre_render(**kwargs):
    logger = getLogger("UI")
    ret = {
        "counter_passed_authbasic": {
            "value": 0,
            "title": "AUTH BASIC",
            "subtitle": "Successful",
            "subtitle_color": "success",
            "svg_color": "success",
        },
        "counter_failed_authbasic": {
            "value": 0,
            "title": "AUTH BASIC",
            "subtitle": "Failed",
            "subtitle_color": "danger",
            "svg_color": "danger",
        },
        "top_authbasic_users": {
            "col-size": "col-12 col-md-6",
            "data": {},
            "order": {
                "column": 1,
                "dir": "desc",
            },
            "svg_color": "primary",
        },
        "top_authbasic_failed_ips": {
            "col-size": "col-12 col-md-6",
            "data": {},
            "order": {
                "column": 1,
                "dir": "desc",
            },
            "svg_color": "warning",
        },
        "list_authbasic_authentications": {
            "col-size": "col-12",
            "data": {},
            "order": {
                "column": 0,
                "dir": "desc",
            },
            "svg_color": "primary",
        },
        "list_authbasic_configured_users": {
            "col-size": "col-12",
            "data": {},
            "order": {
                "column": 0,
                "dir": "asc",
            },
            "svg_color": "info",
        },
    }
    try:
        metrics = kwargs["bw_instances_utils"].get_metrics("authbasic")
        args = kwargs.get("args") or {}
        data_payload = kwargs.get("data") or {}
        service_filter = _normalize_service_name(
            args.get("service") or args.get("server_name") or data_payload.get("service") or data_payload.get("server_name")
        )
        apply_service_filter = bool(service_filter)

        # Get counters
        ret["counter_passed_authbasic"]["value"] = metrics.get("counter_passed_authbasic", 0)
        ret["counter_failed_authbasic"]["value"] = metrics.get("counter_failed_authbasic", 0)

        # Top users / failed IPs come from bounded Top-N trackers (Space-Saving)
        # in metrics_datastore (see authbasic.lua + bunkerweb.top_n). The Lua
        # API surfaces them under `topn_passed_user`, `topn_failed_user`, and
        # `topn_failed_ip`; multi-instance lists need re-coalescing here.
        def _coalesce_topn(dim: str) -> Counter:
            counter: Counter = Counter()
            raw = metrics.get(f"topn_{dim}") if isinstance(metrics, dict) else None
            if isinstance(raw, list):
                for entry in raw:
                    if not isinstance(entry, dict):
                        continue
                    value = entry.get("value")
                    count = entry.get("count")
                    if value in (None, "") or not isinstance(count, (int, float)):
                        continue
                    counter[str(value)] += int(count)
            return counter

        passed_counter = _coalesce_topn("passed_user")
        users_data = {"user": [], "count": []}
        for username, count in passed_counter.most_common():
            users_data["user"].append(username)
            users_data["count"].append(count)
        ret["top_authbasic_users"]["data"] = users_data

        failed_ip_counter = _coalesce_topn("failed_ip")
        ips_data = {"ip": [], "count": []}
        for ip, count in failed_ip_counter.most_common():
            ips_data["ip"].append(ip)
            ips_data["count"].append(count)
        ret["top_authbasic_failed_ips"]["data"] = ips_data

        # Authentication event list now rebuilds from the global blocked
        # `requests` stream (filtered by reason) instead of the dropped
        # `tables/authentications` event array. We surface failed authentication
        # attempts here - successful authentications aren't routed through the
        # blocked-requests pipeline by design.
        list_fields = ["date", "ip", "server_name", "uri", "username", "success", "reason"]
        list_data = {field: [] for field in list_fields}

        requests_payload = kwargs["bw_instances_utils"].get_metrics("requests")
        request_stream = []
        if isinstance(requests_payload, dict):
            request_stream = requests_payload.get("requests") or []

        seen_entries = set()
        for entry in request_stream:
            reason = str(entry.get("reason") or "").lower()
            if reason not in ("authbasic", "auth-basic", "authentication"):
                continue
            entry_service = _normalize_service_name(entry.get("server_name"))
            if apply_service_filter and entry_service != service_filter:
                continue

            entry_key = (
                entry.get("date", 0),
                entry.get("ip", ""),
                entry.get("username") or entry.get("user_agent", ""),
                entry.get("url") or entry.get("uri", ""),
            )
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)

            list_data["date"].append(_format_date(entry.get("date", 0)))
            list_data["ip"].append(entry.get("ip", ""))
            list_data["server_name"].append(entry.get("server_name", ""))
            list_data["uri"].append(entry.get("url") or entry.get("uri", ""))
            list_data["username"].append(entry.get("username", ""))
            # All entries here represent failed authentication (requests stream
            # only carries blocked traffic).
            list_data["success"].append("✗")
            list_data["reason"].append(entry.get("reason", ""))

        ret["list_authbasic_authentications"]["data"] = list_data

        # Configured-users panel: the previous implementation enumerated
        # `counter_passed_user_*` keys, which were unbounded. The bounded
        # `topn_passed_user` tracker carries the same data shape (user → count)
        # within a fixed memory budget; reuse it here.
        configured_users_data = {"scope": [], "username": [], "auth_count": []}
        for username, count in passed_counter.most_common():
            configured_users_data["scope"].append("global")
            configured_users_data["username"].append(username)
            configured_users_data["auth_count"].append(count)
        ret["list_authbasic_configured_users"]["data"] = configured_users_data

    except BaseException as e:
        logger.debug(format_exc())
        logger.error(f"Failed to get authbasic metrics: {e}")
        ret["error"] = str(e)

    return ret


def authbasic(**kwargs):
    pass
