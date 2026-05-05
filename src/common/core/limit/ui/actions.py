from collections import Counter
from logging import getLogger
from traceback import format_exc


def pre_render(**kwargs):
    logger = getLogger("UI")
    ret = {
        "top_limit": {
            "data": {},
            "order": {
                "column": 1,
                "dir": "desc",
            },
            "svg_color": "amber",
        },
    }
    try:
        # Top-limited URIs come from the bounded `topn_uri` Space-Saving tracker
        # in metrics_datastore (see src/common/core/limit/limit.lua and
        # bunkerweb.top_n). When metrics are aggregated across instances the
        # `topn_uri` lists are concatenated, so we coalesce per URL here.
        metrics = kwargs["bw_instances_utils"].get_metrics("limit")
        raw = metrics.get("topn_uri") if isinstance(metrics, dict) else None
        counter: Counter = Counter()
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("value")
                count = entry.get("count")
                if value in (None, "") or not isinstance(count, (int, float)):
                    continue
                counter[str(value)] += int(count)
        data = {"URL": [], "count": []}
        for url, count in counter.most_common():
            data["URL"].append(url)
            data["count"].append(count)
        ret["top_limit"]["data"] = data
    except BaseException as e:
        logger.debug(format_exc())
        logger.error(f"Failed to get limit metrics: {e}")
        ret["error"] = str(e)

    return ret


def limit(**kwargs):
    pass
