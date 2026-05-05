from collections import Counter
from json import loads as json_loads
from logging import getLogger
from operator import itemgetter
from traceback import format_exc
from datetime import datetime


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


def _format_increment_date(raw_value):
    if isinstance(raw_value, (int, float)):
        try:
            return datetime.fromtimestamp(raw_value).isoformat()
        except (OSError, ValueError):
            return datetime.fromtimestamp(0).isoformat()
    try:
        return datetime.fromtimestamp(float(raw_value)).isoformat()
    except (TypeError, ValueError, OSError):
        return str(raw_value or "")


def _flatten_table_increments(raw_increments):
    if raw_increments is None:
        return []
    if isinstance(raw_increments, str):
        try:
            raw_increments = json_loads(raw_increments)
        except ValueError:
            return []
    if isinstance(raw_increments, list):
        return raw_increments
    if isinstance(raw_increments, dict):
        flattened = []
        for value in raw_increments.values():
            if isinstance(value, list):
                flattened.extend(value)
            elif value:
                flattened.append(value)
        return flattened
    return []


def _coalesce_topn(metrics, dim):
    """Coalesce a Top-N list returned by the Lua metrics:api() across instances.

    metrics:api() emits one `topn_<dim>` list per instance. When we aggregate
    across multiple BunkerWeb instances the lists get extended (see
    aggregate_metrics in instance.py), which leaves us with duplicate `value`
    entries that need re-summing. Returns a list of (value, count) tuples
    sorted by count descending.
    """
    raw = metrics.get(f"topn_{dim}")
    if not isinstance(raw, list) or not raw:
        return []
    counter = Counter()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        count = entry.get("count")
        if value in (None, "") or not isinstance(count, (int, float)):
            continue
        counter[str(value)] += int(count)
    return counter.most_common()


def _format_topn(metrics, dim, label):
    """Format coalesced Top-N data for the dashboard panel."""
    formatted = {label: [], "count": []}
    for value, count in _coalesce_topn(metrics, dim):
        formatted[label].append(value)
        formatted["count"].append(count)
    return formatted


def pre_render(**kwargs):
    logger = getLogger("UI")
    ret = {
        "top_bad_behavior_status": {
            "col-size": "col-12 col-md-4",
            "data": {},
            "order": {
                "column": 1,
                "dir": "desc",
            },
            "svg_color": "primary",
        },
        "top_bad_behavior_ips": {
            "col-size": "col-12 col-md-4",
            "data": {},
            "order": {
                "column": 2,
                "dir": "desc",
            },
            "svg_color": "primary",
        },
        "top_bad_behavior_urls": {
            "col-size": "col-12 col-md-4",
            "data": {},
            "order": {
                "column": 3,
                "dir": "desc",
            },
            "svg_color": "primary",
        },
        "list_bad_behavior_history": {
            "col-size": "col-12",
            "data": {},
            "order": {
                "column": 4,
                "dir": "desc",
            },
            "svg_color": "warning",
        },
    }
    try:
        metrics = kwargs["bw_instances_utils"].get_metrics("badbehavior")
        args = kwargs.get("args") or {}
        data_payload = kwargs.get("data") or {}
        service_filter = _normalize_service_name(
            args.get("service") or args.get("server_name") or data_payload.get("service") or data_payload.get("server_name")
        )
        apply_service_filter = bool(service_filter)

        # Status counter is bounded (~60 HTTP codes), still surfaced as a flat
        # `counter_status_<code>` set by badbehavior.lua:106. Keep parsing it
        # as before.
        format_data = [
            {
                "code": int(key.split("_")[2]),
                "count": int(value),
            }
            for key, value in metrics.items()
            if key.startswith("counter_status_")
        ]
        format_data.sort(key=itemgetter("count"), reverse=True)
        data = {"code": [], "count": []}
        for item in format_data:
            data["code"].append(item["code"])
            data["count"].append(item["count"])
        ret["top_bad_behavior_status"]["data"] = data

        # Top-N IPs / URLs come from bounded Space-Saving trackers in the
        # metrics_datastore shdict (see badbehavior.lua + bunkerweb.top_n).
        # The Lua API surfaces them under `topn_ip` and `topn_url`; if multiple
        # instances are aggregated their lists are concatenated and need to be
        # re-coalesced here.
        ret["top_bad_behavior_ips"]["data"] = _format_topn(metrics, "ip", "ip")
        ret["top_bad_behavior_urls"]["data"] = _format_topn(metrics, "url", "url")

        # Bad-behavior history list now rebuilds from the global blocked
        # `requests` stream (filtered by reason) instead of the dropped
        # per-IP `tables/increments_<ip>` entries. The data is structurally
        # identical - same fields, just bounded by METRICS_MAX_BLOCKED_REQUESTS.
        list_fields = [
            "date",
            "id",
            "ip",
            "country",
            "server_name",
            "method",
            "url",
            "status",
            "security_mode",
            "ban_scope",
            "ban_time",
            "threshold",
            "count_time",
        ]
        list_data = {field: [] for field in list_fields}

        requests_payload = kwargs["bw_instances_utils"].get_metrics("requests")
        request_stream = []
        if isinstance(requests_payload, dict):
            request_stream = requests_payload.get("requests") or []

        seen_ids = set()
        for request in _flatten_table_increments(request_stream):
            reason = str(request.get("reason") or "").lower()
            if reason not in ("bad-behavior", "badbehavior"):
                continue
            entry_service = _normalize_service_name(request.get("server_name"))
            if apply_service_filter and entry_service != service_filter:
                continue

            request_id = request.get("id")
            if request_id and request_id in seen_ids:
                continue
            if request_id:
                seen_ids.add(request_id)

            list_data["date"].append(_format_increment_date(request.get("date", 0)))
            list_data["id"].append(request_id or "")
            list_data["ip"].append(request.get("ip", ""))
            list_data["country"].append(request.get("country", ""))
            list_data["server_name"].append(request.get("server_name", ""))
            list_data["method"].append(request.get("method", ""))
            list_data["url"].append(request.get("url", ""))
            list_data["status"].append(request.get("status", ""))
            list_data["security_mode"].append(request.get("security_mode", ""))
            list_data["ban_scope"].append(request.get("ban_scope", ""))
            list_data["ban_time"].append(str(request.get("ban_time", "")))
            list_data["threshold"].append(str(request.get("threshold", "")))
            list_data["count_time"].append(str(request.get("count_time", "")))

        ret["list_bad_behavior_history"]["data"] = list_data

    except BaseException as e:
        logger.debug(format_exc())
        logger.error(f"Failed to get badbehavior metrics: {e}")
        ret["error"] = str(e)

    return ret


def badbehavior(**kwargs):
    pass
