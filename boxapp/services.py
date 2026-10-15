"""Everything the admin pages ask of the box (the old FastAPI routers, as plain functions)."""
import logging
import re
from datetime import datetime
from urllib.parse import quote

import httpx
from django.conf import settings
from django.core.cache import cache

from . import mapping
from .b4h import B4HError
from .box import box

log = logging.getLogger("b4h")

MAX_PAGE = settings.BOX_MAX_PAGE_SIZE
MAX_RECORDS = 5000  # safety cap when collecting a whole time range

MATCHED = "face_comparison_successful"
# Only minor types the box lists in GET /device_alarm/alarm_cap. An unknown one crashes the box's web server.
RECOGNITION_MINORS = [(MATCHED, "Matched"), ("stranger", "Stranger")]

CAPTURE_TARGETS = [("all", "All types"), ("face", "Face"), ("body", "Body")]
_CAPTURE_MINORS = {
    "all": ["face_capture", "body_capture"],
    "face": ["face_capture"],
    "body": ["body_capture"],
}
# The box rejects the request (code 1073741831 "not_support") unless a second alarm_type entry
# for "structure" is present alongside face_basic_business.
_STRUCTURE_ALARM_TYPE = {
    "major_type": "structure",
    "minor_type": ["face", "pedestrian", "vehicle", "non_motor", "plate"],
}


def to_ms(dt: datetime) -> int:
    """Timezone-aware datetime -> epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def to_s(dt: datetime) -> int:
    """Timezone-aware datetime -> epoch seconds (the box's log times are in seconds)."""
    return int(dt.timestamp())


# ---------- devices / people ----------

def devices() -> list[dict]:
    """Cameras configured on the box (id + name only: the raw config holds RTSP passwords)."""
    data = box.call("POST", "/device_access/device_config", {"offset": 0, "size": 100})
    items = mapping.find_list(data) if not isinstance(data, list) else data
    return [{"device_id": d.get("device_id"), "device_name": d.get("device_name")} for d in items or []]


def device_names() -> dict[str, str]:
    """device_id -> name, cached briefly (the list rarely changes)."""
    def load() -> dict[str, str]:
        try:
            return {str(d["device_id"]): d["device_name"] for d in devices()}
        except (B4HError, httpx.HTTPError):
            return {}
    return cache.get_or_set("box_device_names", load, 60)


def people() -> list[dict]:
    """Everyone currently in the box's face library (id + name only)."""
    out: list[dict] = []
    offset = 0
    while True:
        data = box.call("POST", "/face_manager/person/query", {
            "offset": offset,
            "size": MAX_PAGE,
            "get_feature": False,  # features are large and not needed here
        }) or {}
        batch = data.get("person_list") or []
        out += [
            {"person_id": str(p.get("person_id", "")), "name": (p.get("person_info") or {}).get("name", "")}
            for p in batch
        ]
        offset += len(batch)
        if not batch or offset >= int(data.get("total_count") or 0):
            break
    return out


# ---------- recognition ----------

def _alarm_history(start: datetime, end: datetime, alarm_types: list[dict], page: int, size: int) -> dict:
    return box.call("POST", "/device_alarm/alarm_history", {
        "offset": (page - 1) * size,
        "size": size,
        "query_condition": {
            "start_time": str(to_ms(start)),
            "end_time": str(to_ms(end)),
            "alarm_type": alarm_types,
        },
    }) or {}


def _recognition_rows(start, end, minor, page, size) -> tuple[list[dict], int]:
    data = _alarm_history(start, end, [{"major_type": settings.RECOG_MAJOR, "minor_type": [minor]}], page, size)
    records = mapping.find_list(data)
    try:
        total = int(float(mapping.find(data, ["total", "total_num", "total_count", "count"]) or 0))
    except (TypeError, ValueError):
        total = 0
    rows = [mapping.to_recognition_row(r, i) for i, r in enumerate(records)]
    return rows, total or len(records)


def fetch_recognition(start, end, minor, page, size, hide_deleted=True) -> tuple[list[dict], int]:
    """
    Strangers: passed straight through, paginated by the box.

    Matched: the box keeps its alarm history even after a person is deleted from the face library,
    so pull every record in the time range, drop the ones whose person no longer exists, and
    paginate what's left here. That way deleted people's rows are gone and totals are correct.
    """
    if minor != MATCHED or not hide_deleted:
        return _recognition_rows(start, end, minor, page, size)

    try:
        library = people()
    except (B4HError, httpx.HTTPError):
        # Couldn't reach the person library: show the box's data unfiltered rather than an empty table.
        return _recognition_rows(start, end, minor, page, size)

    norm = lambda s: str(s or "").strip().lower()  # noqa: E731
    ids = {p["person_id"] for p in library if p["person_id"]}
    names = {norm(p["name"]) for p in library if norm(p["name"])}

    def still_exists(r: dict) -> bool:
        return r["person_id"] in ids if r["person_id"] else norm(r["name"]) in names

    first_rows, first_total = _recognition_rows(start, end, minor, 1, MAX_PAGE)
    all_rows = list(first_rows)
    box_total = min(first_total, MAX_RECORDS)
    p = 2
    while len(all_rows) < box_total and first_rows:
        rows, _ = _recognition_rows(start, end, minor, p, MAX_PAGE)
        if not rows:
            break
        all_rows += rows
        p += 1

    kept = [r for r in all_rows if still_exists(r)]
    frm = (page - 1) * size
    return kept[frm:frm + size], len(kept)


# ---------- captures ----------

def fetch_captures(start, end, target_type, page, size) -> tuple[list[dict], int]:
    data = _alarm_history(
        start, end,
        [{"major_type": settings.RECOG_MAJOR, "minor_type": _CAPTURE_MINORS[target_type]}, _STRUCTURE_ALARM_TYPE],
        page, size,
    )
    names = device_names()
    rows = [mapping.to_capture_row(mapping.normalize_capture(a), names) for a in data.get("list", [])]
    return rows, int(data.get("total_count") or len(rows))


# ---------- live video ----------

def to_substream(url: str) -> str:
    """Hikvision: .../channels/201 (main stream) -> .../channels/202 (sub stream)."""
    return re.sub(r"(?i)(/channels/\d+)01$", r"\g<1>02", url)


def _with_credentials(url: str, rtsp_param: dict) -> str:
    """
    Make sure the RTSP URL carries usable credentials.

    * The box may keep user/password in separate fields and leave them out of `url` -> add them.
    * A password containing '@', ':', '/' or '#' written raw in the URL breaks parsing -> percent-encode it.
      The host starts after the LAST '@' (camera paths never contain one), so everything before it is userinfo.
    """
    m = re.match(r"(?i)^(rtsps?://)(.*)$", url)
    if not m:
        return url                                       # a file path or other source: leave untouched
    scheme, rest = m.groups()
    at = rest.rfind("@")
    if at >= 0:
        userinfo, tail = rest[:at], rest[at + 1:]
        if "%" in userinfo:                              # already encoded
            return url
        user, _, pwd = userinfo.partition(":")
    else:
        user, pwd, tail = rtsp_param.get("user"), rtsp_param.get("password"), rest
        if not user:
            return url
    cred = quote(str(user), safe="")
    if pwd is not None and str(pwd) != "":
        cred += ":" + quote(str(pwd), safe="")
    return f"{scheme}{cred}@{tail}"


def mask_url(url: str) -> str:
    """rtsp://user:pass@host:554/path -> rtsp://***@host:554/path (safe for logs and error messages)."""
    m = re.match(r"(?i)^([a-z][a-z0-9+.-]*://)(.*)$", url)
    if not m:
        return url
    scheme, rest = m.groups()
    at = rest.rfind("@")
    return f"{scheme}***@{rest[at + 1:]}" if at >= 0 else url


def _rtsp_urls() -> dict[int, str]:
    data = box.call("POST", "/device_access/device_config", {"offset": 0, "size": 100})
    items = data if isinstance(data, list) else mapping.find_list(data)
    out: dict[int, str] = {}
    for d in items or []:
        param = d.get("rtsp_param") or {}
        url = param.get("url")
        if url and d.get("device_id") is not None:
            out[int(d["device_id"])] = _with_credentials(url, param)
    return out


def _cached_rtsp_urls() -> dict[int, str]:
    # In-process cache only (LocMem). These URLs contain the camera password.
    # An empty answer is never cached: the box sometimes returns an empty list right after a restart,
    # and caching that would show "no RTSP URL" for 5 minutes.
    urls = cache.get("box_rtsp_urls")
    if urls is None:
        urls = _rtsp_urls()
        if urls:
            cache.set("box_rtsp_urls", urls, 300)
    return urls


def camera_ids() -> set[int]:
    """Device ids that have an RTSP URL on the box."""
    return set(_cached_rtsp_urls())


def camera_rtsp_url(device_id: int) -> str | None:
    """RTSP URL (with the camera password). Server-side only: never send it to the browser."""
    url = _cached_rtsp_urls().get(device_id)
    if url and settings.LIVE_SUBSTREAM:
        url = to_substream(url)
    return url


# ---------- box logs ----------

_TOKEN = re.compile(r"\{(\w+)(?::(str))?\}")


def _fill(node, values: dict):
    """Replace "{token}" strings in the configured request body (see settings.B4H_LOG_BODY)."""
    if isinstance(node, dict):
        return {k: _fill(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill(v, values) for v in node]
    if isinstance(node, str) and (m := _TOKEN.fullmatch(node)):
        value = values[m.group(1)]
        return str(value) if m.group(2) else value
    return node


def _log_body(start: datetime, end: datetime, page: int, size: int) -> dict:
    return _fill(settings.B4H_LOG_BODY, {
        "offset": (page - 1) * size, "size": size, "page": page,
        "start_s": to_s(start), "end_s": to_s(end),
        "start_ms": to_ms(start), "end_ms": to_ms(end),
    })


def fetch_logs(start, end, page, size) -> tuple[list[dict], int]:
    """One page of box logs, newest first."""
    data = box.call("POST", settings.B4H_LOG_PATH, _log_body(start, end, page, size)) or {}
    records = data.get("logs_data") or []
    total = int(data.get("total_count") or len(records))
    return [mapping.to_log_row(r) for r in records], total


def log_file() -> tuple[bytes, str]:
    """Full log download exactly as the box sends it -> (body, content-type)."""
    return box.get_bytes(settings.B4H_LOG_FILE_PATH)


# ---------- algorithm packages ----------

def algorithms() -> list[dict]:
    """Algorithm packages installed on the box."""
    data = box.call("POST", settings.B4H_ALG_PATH, settings.B4H_ALG_BODY) or {}
    return [mapping.to_algorithm_row(a) for a in data.get("list") or []]
