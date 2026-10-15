"""
Next-hour event forecast, run by a background agent.

What it predicts
----------------
For every (camera, event type) it predicts `next_hour_event_count`: how many events are expected in the
clock hour that is just starting. If the prediction is far above what is normal for that hour of the
week, the page recommends that an operator investigates.

One training row = one past hour T, with the features known at the start of T:

    hour                      hour of day of T                      (14)
    day_of_week               Monday=0 ... Sunday=6                 (2 = Wednesday)
    previous_hour_event_count events in the hour before T           (8)
    previous_day_event_count  events in the same hour yesterday     (10)
    event_type                face_comparison_successful, stranger, face_capture, body_capture, movement
    device_id                 camera
    target: next_hour_event_count = events actually counted in T    (never invented: it is counted
                                                                      from the box's timestamped history)

What the agent does every hour (a few minutes after the hour, so the box has finished the previous one)
-------------------------------------------------------------------------------------------------------
1. COLLECT  pull new timestamped events from the box (alarm_history) and the portal's own movement
            alarms, bucket them per hour / camera / type into HourlyEventCount. First run backfills
            FORECAST_BACKFILL_DAYS. Re-running is safe: whole hours are replaced, never added twice.
2. TRAIN    fit two Poisson models (gradient boosting and a log-linear one), score them and a plain
            "same hour of the week" average on the most recent hours none has seen, and use the most
            accurate. A model is only used when it really beats the average.
3. PREDICT  forecast the coming hour for every active camera + event type, compare with normal, and
            store the result with a plain-language recommendation.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone

import numpy as np
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from . import mapping, services
from .models import EventForecast, ForecastState, HourlyEventCount, MovementAlarmEvent

log = logging.getLogger("b4h.forecast")

HOUR = timedelta(hours=1)
UTC = dt_timezone.utc
MOVEMENT = "movement"
MAX_RECORDS_PER_WINDOW = 20000      # safety cap per one-day window
MIN_HISTORY_HOURS = 24              # a series needs a day of history before it can be forecast
HOLDOUT_MIN_ROWS = 24
ACTIVE_WITHIN_DAYS = 14             # series with no event for this long are not forecast

EVENT_LABELS = {
    "face_comparison_successful": "Face recognition",
    "stranger": "Stranger",
    "face_capture": "Face capture",
    "body_capture": "Body capture",
    MOVEMENT: "Restricted movement",
}


def label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type.replace("_", " ").capitalize())


def floor_hour(dt: datetime) -> datetime:
    """Start of the local clock hour containing dt (aware datetime, returned in the portal's time zone)."""
    return timezone.localtime(dt).replace(minute=0, second=0, microsecond=0)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)


# ================================================================================================
# 1. COLLECT
# ================================================================================================

def _sources() -> dict[str, list[dict]]:
    """alarm_type filters per box source. Same shapes services.py already uses for the pages."""
    return {
        "recognition": [{"major_type": settings.RECOG_MAJOR, "minor_type": [services.MATCHED, "stranger"]}],
        "capture": [
            {"major_type": settings.RECOG_MAJOR, "minor_type": ["face_capture", "body_capture"]},
            services._STRUCTURE_ALARM_TYPE,
        ],
    }


def _record_time(rec: dict) -> datetime | None:
    v = mapping.find(rec, ["time_ms", "capture_time", "alarm_time", "timestamp", "time"])
    try:
        ms = float(v)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    if ms < 1e12:
        ms *= 1000        # seconds -> ms
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _record_device(rec: dict) -> int | None:
    v = (rec.get("additional") or {}).get("device_id")
    if v is None:
        v = mapping.find(rec, ["device_id", "channel_id"])
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fetch_window(alarm_types: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Every alarm record in [start, end), 30 per request (the box's limit)."""
    out: list[dict] = []
    page, size = 1, services.MAX_PAGE
    while len(out) < MAX_RECORDS_PER_WINDOW:
        data = services._alarm_history(start, end, alarm_types, page, size)
        batch = mapping.find_list(data)
        out += batch
        try:
            total = int(float(mapping.find(data, ["total_count", "total", "total_num", "count"]) or 0))
        except (TypeError, ValueError):
            total = 0
        if not batch or len(batch) < size or (total and len(out) >= total):
            break
        page += 1
        time.sleep(0.05)       # be gentle: the box answers one request at a time and the portal shares it
    else:
        log.warning("History window %s..%s hit the %d record cap; counts for it are incomplete",
                    start, end, MAX_RECORDS_PER_WINDOW)
    return out


def sync_source(name: str, now: datetime | None = None) -> int:
    """Bring HourlyEventCount for one box source up to the last completed hour. Returns records counted."""
    now = now or timezone.now()
    alarm_types = _sources()[name]
    minors = [m for t in alarm_types if t["major_type"] == settings.RECOG_MAJOR for m in t["minor_type"]]
    state, _ = ForecastState.objects.get_or_create(key=f"sync:{name}")
    end = floor_hour(now)                                    # only completed hours are counted
    cursor = timezone.localtime(state.synced_until) if state.synced_until else end - timedelta(days=settings.FORECAST_BACKFILL_DAYS)
    counted = 0
    while cursor < end:
        win_end = min(cursor + timedelta(days=1), end)
        records = _fetch_window(alarm_types, cursor, win_end)
        buckets: Counter = Counter()
        for rec in records:
            t, dev = _record_time(rec), _record_device(rec)
            minor = (rec.get("additional") or {}).get("alarm_minor")
            if t is None or dev is None or minor not in minors:
                continue
            if not (cursor <= t < win_end):                  # the box may return edge records twice
                continue
            buckets[(dev, minor, _utc(floor_hour(t)))] += 1
        with transaction.atomic():
            HourlyEventCount.objects.filter(
                event_type__in=minors, hour_start__gte=cursor, hour_start__lt=win_end,
            ).delete()
            HourlyEventCount.objects.bulk_create(
                HourlyEventCount(device_id=d, event_type=m, hour_start=h, count=c) for (d, m, h), c in buckets.items()
            )
            state.synced_until = win_end
            state.save(update_fields=["synced_until", "updated_at"])
        counted += sum(buckets.values())
        cursor = win_end
    return counted


def sync_movement(now: datetime | None = None) -> int:
    """Portal movement alarms live in our own database: recount the recent days (cheap, idempotent)."""
    now = now or timezone.now()
    end = floor_hour(now)
    start = end - timedelta(days=settings.FORECAST_BACKFILL_DAYS)
    buckets: Counter = Counter()
    for dev, t in MovementAlarmEvent.objects.filter(detected_at__gte=start, detected_at__lt=end).values_list("device_id", "detected_at"):
        buckets[(dev, _utc(floor_hour(t)))] += 1
    with transaction.atomic():
        HourlyEventCount.objects.filter(event_type=MOVEMENT, hour_start__gte=start, hour_start__lt=end).delete()
        HourlyEventCount.objects.bulk_create(
            HourlyEventCount(device_id=d, event_type=MOVEMENT, hour_start=h, count=c) for (d, h), c in buckets.items()
        )
    return sum(buckets.values())


# ================================================================================================
# 2. FEATURES + NORMAL
# ================================================================================================

def load_series() -> dict[tuple[int, str], dict[datetime, int]]:
    """{(device_id, event_type): {hour_start_utc: count}} from the database."""
    series: dict[tuple[int, str], dict[datetime, int]] = defaultdict(dict)
    for dev, et, hs, c in HourlyEventCount.objects.values_list("device_id", "event_type", "hour_start", "count"):
        series[(dev, et)][_utc(hs)] = c
    return series


def _row(hour_dt: datetime, prev_hour: int, prev_day: int, et: str, dev: int) -> list:
    local = timezone.localtime(hour_dt)
    return [local.hour, local.weekday(), prev_hour, prev_day, et, dev]


def build_rows(series: dict, end_utc: datetime) -> list[dict]:
    """
    One row per (series, past hour T). `end_utc` is the start of the current (incomplete) hour: the last
    complete hour is end-1h. Hours with no stored row count as 0.
    """
    rows = []
    for (dev, et), d in series.items():
        first = min(d)
        n = int((end_utc - first) / HOUR)
        if n < MIN_HISTORY_HOURS + 1:
            continue
        get = lambda t: d.get(t, 0)   # noqa: E731
        for i in range(24, n):
            t = first + i * HOUR
            rows.append({
                "series": (dev, et), "t": t,
                "x": _row(t, get(t - HOUR), get(t - 24 * HOUR), et, dev),
                "y": get(t),
            })
    return rows


class SeasonalNormal:
    """
    "What is normal for this hour": the mean count for the same hour of the week (e.g. Wednesdays 14:00),
    falling back to the same hour of the day, then the overall mean, when there are too few samples.
    Spread is floored at sqrt(mean) (count data) and 1.
    """

    MIN_SAMPLES = 3

    def __init__(self, d: dict[datetime, int], first: datetime, last_exclusive: datetime):
        how, hod, allv = defaultdict(list), defaultdict(list), []
        n = int((last_exclusive - first) / HOUR)
        for i in range(max(n, 0)):
            t = first + i * HOUR
            c = d.get(t, 0)
            local = timezone.localtime(t)
            how[(local.weekday(), local.hour)].append(c)
            hod[local.hour].append(c)
            allv.append(c)
        self.how, self.hod, self.all = how, hod, allv

    @staticmethod
    def _stats(v):
        a = np.asarray(v, dtype=float)
        mean = float(a.mean()) if len(a) else 0.0
        return mean, max(float(a.std()) if len(a) else 0.0, math.sqrt(max(mean, 0.0)), 1.0)

    def at(self, hour_dt: datetime) -> tuple[float, float]:
        local = timezone.localtime(hour_dt)
        for v in (self.how.get((local.weekday(), local.hour)), self.hod.get(local.hour)):
            if v and len(v) >= self.MIN_SAMPLES:
                return self._stats(v)
        return self._stats(self.all)


# ================================================================================================
# 3. TRAIN
# ================================================================================================
# Three candidates are scored on hours none of them has seen; the most accurate wins, and the plain
# seasonal average wins if neither model beats it.
#   gbm  gradient-boosted trees (Poisson loss): good at hour/weekday patterns and interactions, but a tree
#        cannot predict beyond counts it has seen, so it under-reacts to a genuine surge.
#   glm  Poisson log-linear model: a daily profile per camera+event type, a weekday effect, and the previous
#        hour/day on a log scale, so a surge in the last hour scales the forecast up multiplicatively.

class _GbmModel:
    name = "gbm"

    def fit(self, rows: list[dict]):
        self.type_codes = {t: i for i, t in enumerate(sorted({r["series"][1] for r in rows}))}
        self.dev_codes = {d: i for i, d in enumerate(sorted({r["series"][0] for r in rows}))}
        self.model = _new_gbm().fit(self._x([r["x"] for r in rows]), np.array([r["y"] for r in rows], dtype=float))
        return self

    def _x(self, xs: list[list]) -> np.ndarray:
        """[hour, dow, prev_hour, prev_day, type_code, device_code]; unseen type/device -> NaN (missing)."""
        out = np.empty((len(xs), 6), dtype=float)
        for i, (h, dow, ph, pd_, et, dev) in enumerate(xs):
            out[i] = [h, dow, ph, pd_, self.type_codes.get(et, np.nan), self.dev_codes.get(dev, np.nan)]
        return out

    def predict(self, xs: list[list]) -> np.ndarray:
        return np.clip(self.model.predict(self._x(xs)), 0, None)


class _GlmModel:
    name = "glm"

    @staticmethod
    def _keys(x: list) -> list[tuple]:
        h, dow, _, _, et, dev = x
        return [("series", et, dev), ("series_hour", et, dev, h), ("dow", dow)]

    def fit(self, rows: list[dict]):
        from sklearn.linear_model import PoissonRegressor

        self.cols: dict[tuple, int] = {}
        for r in rows:
            for k in self._keys(r["x"]):
                self.cols.setdefault(k, len(self.cols))
        self.model = PoissonRegressor(alpha=1.0, max_iter=1000).fit(
            self._x([r["x"] for r in rows]), np.array([r["y"] for r in rows], dtype=float))
        return self

    def _x(self, xs: list[list]) -> np.ndarray:
        out = np.zeros((len(xs), len(self.cols) + 2))
        for i, x in enumerate(xs):
            for k in self._keys(x):
                j = self.cols.get(k)
                if j is not None:
                    out[i, j] = 1.0
            out[i, -2], out[i, -1] = math.log1p(x[2]), math.log1p(x[3])
        return out

    def predict(self, xs: list[list]) -> np.ndarray:
        return np.clip(self.model.predict(self._x(xs)), 0, None)


def _new_gbm():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(
        loss="poisson", categorical_features=[1, 4, 5], learning_rate=0.05, max_iter=250,
        min_samples_leaf=10, l2_regularization=1.0, random_state=0,
        monotonic_cst=[0, 0, 1, 1, 0, 0],   # more events in the previous hour/day never means fewer expected
    )


def train(series: dict, end_utc: datetime) -> dict:
    """
    Fit and validate. Returns {model, use_model, metrics}. `model` is None (and the forecast is the seasonal
    average) when there is too little data or no model beats the average on unseen hours.
    """
    import joblib

    rows = build_rows(series, end_utc)
    metrics: dict = {"rows": len(rows), "series": len(series), "trained_at": timezone.now().isoformat(timespec="seconds"),
                     "used": "baseline"}
    bundle = {"model": None, "use_model": False, "metrics": metrics}
    if len(rows) < settings.FORECAST_MIN_TRAIN_ROWS:
        metrics["note"] = f"Only {len(rows)} training hours (need {settings.FORECAST_MIN_TRAIN_ROWS}): using the seasonal average."
        return bundle

    times = sorted({r["t"] for r in rows})
    # hold out the most recent hours (at least 2 days, or 20% of the span) to test on data nobody has seen
    hold_hours = max(48, int(len(times) * 0.2))
    cutoff = times[-hold_hours] if len(times) > hold_hours else times[-1]
    train_rows = [r for r in rows if r["t"] < cutoff]
    test_rows = [r for r in rows if r["t"] >= cutoff]
    if len(train_rows) < settings.FORECAST_MIN_TRAIN_ROWS or len(test_rows) < HOLDOUT_MIN_ROWS:
        metrics["note"] = "Not enough recent hours to validate a model: using the seasonal average."
        return bundle

    actual = np.array([r["y"] for r in test_rows], dtype=float)
    xs = [r["x"] for r in test_rows]
    normals = {key: SeasonalNormal(d, min(d), cutoff) for key, d in series.items()}     # training hours only: no peeking
    mae = {"baseline": float(np.abs(np.array([normals[r["series"]].at(r["t"])[0] for r in test_rows]) - actual).mean())}
    for cls in (_GbmModel, _GlmModel):
        try:
            mae[cls.name] = float(np.abs(cls().fit(train_rows).predict(xs) - actual).mean())
        except Exception as e:      # a candidate failing must not stop the others
            log.warning("Forecast candidate %s failed: %r", cls.name, e)
    best = min(mae, key=mae.get)
    metrics.update(holdout_hours=hold_hours, holdout_rows=len(test_rows),
                   mae={k: round(v, 3) for k, v in mae.items()}, mae_baseline=round(mae["baseline"], 3))
    if best == "baseline":
        metrics["note"] = "Neither model beat the seasonal average on unseen hours: using the average."
        return bundle

    metrics.update(used=best, mae_model=round(mae[best], 3),
                   note=f"The {best} model beat the seasonal average on unseen hours: using it.")
    bundle["model"] = {"gbm": _GbmModel, "glm": _GlmModel}[best]().fit(rows)      # refit on everything, newest hours included
    bundle["use_model"] = True
    try:
        joblib.dump({"model": bundle["model"], "metrics": metrics}, settings.FORECAST_MODEL_PATH)
    except OSError as e:
        log.warning("Could not save the model file: %s", e)
    return bundle


# ================================================================================================
# 4. PREDICT
# ================================================================================================

def is_investigate(predicted: float, normal: float, sd: float) -> bool:
    """Much higher than normal: above normal+Z*sd, at least RATIO x normal, and at least MIN_DELTA more events."""
    return (predicted >= normal + settings.FORECAST_Z * sd
            and predicted >= normal * settings.FORECAST_RATIO
            and predicted - normal >= settings.FORECAST_MIN_DELTA)


def recommendation(status: str, predicted: float, normal: float, ratio: float, device_name: str, et: str, hour_dt: datetime) -> str:
    local = timezone.localtime(hour_dt)
    span = f"{local:%H:00}-{(local + HOUR):%H:00}"
    if status == EventForecast.STATUS_INVESTIGATE:
        return (f"Expect about {predicted:.0f} {label(et).lower()} events on {device_name} between {span}, "
                f"{ratio:.1f}x the normal {normal:.0f}. An operator should investigate: open the live view for this "
                f"camera and review its latest captures and recognitions.")
    return f"About {predicted:.0f} {label(et).lower()} events expected on {device_name} between {span} (normal {normal:.0f}). No action needed."


def predict_next_hour(series: dict, bundle: dict, end_utc: datetime) -> list[EventForecast]:
    """Forecast the hour starting at end_utc for every series active in the last ACTIVE_WITHIN_DAYS."""
    try:
        names = services.device_names()
    except Exception:           # camera names are cosmetic: never lose a forecast over them
        names = {}
    active_since = end_utc - timedelta(days=ACTIVE_WITHIN_DAYS)
    todo = []
    for (dev, et), d in series.items():
        first = min(d)
        if (end_utc - first) / HOUR < MIN_HISTORY_HOURS or max(d) < active_since:
            continue
        todo.append(((dev, et), d, first))
    if not todo:
        return []

    xs = []
    for (dev, et), d, _ in todo:
        xs.append(_row(end_utc, d.get(end_utc - HOUR, 0), d.get(end_utc - 24 * HOUR, 0), et, dev))
    model_pred = None
    if bundle["use_model"] and bundle["model"] is not None:
        model_pred = bundle["model"].predict(xs)

    out = []
    for i, ((dev, et), d, first) in enumerate(todo):
        normal, sd = SeasonalNormal(d, first, end_utc).at(end_utc)
        predicted = float(model_pred[i]) if model_pred is not None else normal
        ratio = predicted / normal if normal > 0 else (predicted if predicted > 0 else 1.0)
        status = EventForecast.STATUS_INVESTIGATE if is_investigate(predicted, normal, sd) else EventForecast.STATUS_NORMAL
        name = names.get(str(dev), f"Camera {dev}")
        fc, _ = EventForecast.objects.update_or_create(
            target_hour=end_utc, device_id=dev, event_type=et,
            defaults=dict(predicted=round(predicted, 2), normal=round(normal, 2), normal_sd=round(sd, 2), ratio=round(ratio, 2),
                          status=status, model_used=bundle["model"].name if model_pred is not None else "baseline", actual=None,
                          recommendation=recommendation(status, predicted, normal, ratio, name, et, end_utc)),
        )
        out.append(fc)
    return out


def fill_actuals(end_utc: datetime) -> int:
    """Once a forecast's hour is over, record what really happened so the page can show forecast vs actual."""
    n = 0
    for fc in EventForecast.objects.filter(actual__isnull=True, target_hour__lt=end_utc):
        row = HourlyEventCount.objects.filter(device_id=fc.device_id, event_type=fc.event_type, hour_start=fc.target_hour).first()
        fc.actual = row.count if row else 0
        fc.save(update_fields=["actual"])
        n += 1
    return n


# ================================================================================================
# 5. THE AGENT
# ================================================================================================

def run_cycle(now: datetime | None = None) -> dict:
    """collect -> train -> predict. Never raises: problems are returned in summary['errors']."""
    now = now or timezone.now()
    end_utc = _utc(floor_hour(now))
    summary: dict = {"started": timezone.now().isoformat(timespec="seconds"), "errors": [], "target_hour": end_utc.isoformat()}

    for name in _sources():
        try:
            summary[f"counted_{name}"] = sync_source(name, now)
        except Exception as e:      # box down (B4HError, network), bad reply, ...: keep going with what we have
            log.warning("Forecast: could not sync %s: %r", name, e)
            summary["errors"].append(f"{name}: {e!r}")
    try:
        summary["counted_movement"] = sync_movement(now)
    except Exception as e:
        summary["errors"].append(f"movement: {e!r}")

    series = load_series()
    summary["series"] = len(series)
    if not series:
        summary["note"] = "No timestamped event history yet."
    else:
        try:
            bundle = train(series, end_utc)
            summary["model"] = bundle["metrics"]
            forecasts = predict_next_hour(series, bundle, end_utc)
            summary["forecasts"] = len(forecasts)
            summary["flagged"] = sum(f.status == EventForecast.STATUS_INVESTIGATE for f in forecasts)
            if not summary["errors"]:
                summary["actuals_filled"] = fill_actuals(end_utc)
        except Exception as e:
            log.exception("Forecast training/prediction failed")
            summary["errors"].append(f"forecast: {e!r}")
    summary["finished"] = timezone.now().isoformat(timespec="seconds")
    state, _ = ForecastState.objects.get_or_create(key="agent")
    state.data = summary
    state.save()
    return summary


class ForecastAgent:
    """
    Background thread inside the portal process (same pattern as the movement alarm workers, so run ONE
    server process). Wakes a few minutes after every full hour and runs one cycle.
    """

    _lock = threading.Lock()
    _run_lock = threading.Lock()
    _thread: threading.Thread | None = None
    running = False

    @classmethod
    def start(cls) -> None:
        with cls._lock:
            if cls._thread and cls._thread.is_alive():
                return
            cls._thread = threading.Thread(target=cls._loop, name="forecast-agent", daemon=True)
            cls._thread.start()

    @classmethod
    def _next_wake(cls) -> float:
        now = timezone.now()
        nxt = floor_hour(now) + HOUR + timedelta(minutes=settings.FORECAST_RUN_MINUTE)
        return max((nxt - now).total_seconds(), 60.0)

    @classmethod
    def _loop(cls) -> None:
        time.sleep(20)                                   # let the server finish starting
        first = True
        while True:
            try:
                if first:
                    first = False
                    cls.run_once(only_if_stale=True)
                else:
                    cls.run_once()
            except Exception:
                log.exception("Forecast agent cycle crashed")
            finally:
                close_old_connections()
            time.sleep(cls._next_wake())

    @classmethod
    def run_once(cls, only_if_stale: bool = False) -> dict | None:
        if not cls._run_lock.acquire(blocking=False):
            return None                                  # a cycle is already running
        cls.running = True
        try:
            if only_if_stale:
                cur = _utc(floor_hour(timezone.now()))
                if EventForecast.objects.filter(target_hour=cur).exists():
                    return None
            return run_cycle()
        finally:
            cls.running = False
            cls._run_lock.release()

    @classmethod
    def run_async(cls) -> bool:
        """'Run now' button: start a cycle in the background unless one is already running."""
        if cls._run_lock.locked():
            return False
        threading.Thread(target=lambda: (cls.run_once(), close_old_connections()), name="forecast-run-now", daemon=True).start()
        return True
