"""Tests for the next-hour event forecast agent (boxapp/forecast.py)."""
import tempfile
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path
from unittest.mock import patch

import numpy as np
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

import mock_box
from boxapp import forecast
from boxapp.box import box
from boxapp.models import EventForecast, ForecastState, HourlyEventCount, MovementAlarmEvent, MovementAlarmRule

FACE = "face_comparison_successful"
H = timedelta(hours=1)

_tmp = tempfile.TemporaryDirectory()
_override = override_settings(FORECAST_MODEL_PATH=Path(_tmp.name) / "forecast_model.pkl")   # never touch the real file


def setUpModule():
    _override.enable()


def tearDownModule():
    _override.disable()
    _tmp.cleanup()


def local_hour(y, m, d, h):
    return timezone.make_aware(datetime(y, m, d, h))


def put(dev, et, hour_dt, count):
    HourlyEventCount.objects.update_or_create(device_id=dev, event_type=et, hour_start=hour_dt.astimezone(dt_tz.utc),
                                              defaults={"count": count})


def seed_bursty(end_utc, days=21, seed=0):
    """
    Two cameras with a daily rhythm (busy 09:00-17:00 on camera 1) plus bursts: when a burst starts it lasts
    2-4 hours at about 4x the rate, so the previous hour's count really does tell you about the next hour.
    Zero-count hours are NOT stored, like the real collector.
    """
    rng = np.random.default_rng(seed)
    for dev in (1, 2):
        burst_left = 0
        for i in range(days * 24):
            t = end_utc - (days * 24 - i) * H
            hr = timezone.localtime(t).hour
            base = (6.0 if 9 <= hr <= 17 else 1.0) if dev == 1 else 2.0
            if burst_left == 0 and rng.random() < 0.06:
                burst_left = int(rng.integers(2, 5))
            lam = base * (4.0 if burst_left else 1.0)
            burst_left = max(burst_left - 1, 0)
            c = int(rng.poisson(lam))
            if c:
                put(dev, FACE, t, c)


class FeatureTest(TestCase):
    def test_training_row_matches_the_example(self):
        """Wed 14:00, previous hour 8, same hour yesterday 10, actual next count 25 -> exactly one such row."""
        T = local_hour(2026, 9, 16, 14)                       # 2026-09-16 is a Wednesday
        end = T + 5 * H
        for i in range(0, 120):                               # 5 days of quiet history (1 event/hour)
            put(1, FACE, T - (96 - i) * H + 0 * H, 1)
        put(1, FACE, T - 24 * H, 10)                          # Tuesday 14:00
        put(1, FACE, T - H, 8)                                # Wednesday 13:00
        put(1, FACE, T, 25)                                   # Wednesday 14:00 = the target
        series = forecast.load_series()
        rows = [r for r in forecast.build_rows(series, end.astimezone(dt_tz.utc)) if r["t"] == T.astimezone(dt_tz.utc)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["x"], [14, 2, 8, 10, FACE, 1])   # hour, Wednesday, prev hour, prev day, type, device
        self.assertEqual(rows[0]["y"], 25)

    def test_empty_hours_count_as_zero(self):
        T = local_hour(2026, 9, 16, 14)
        for i in range(0, 60):
            if i != 30:
                put(1, FACE, T + i * H, 3)                    # hour 30 has no row at all
        series = forecast.load_series()
        rows = {r["t"]: r for r in forecast.build_rows(series, (T + 60 * H).astimezone(dt_tz.utc))}
        missing = (T + 30 * H).astimezone(dt_tz.utc)
        self.assertEqual(rows[missing]["y"], 0)
        self.assertEqual(rows[missing + H]["x"][2], 0)        # next row's previous_hour_event_count


class DetectorTest(TestCase):
    def test_much_higher_than_normal_is_flagged(self):
        self.assertTrue(forecast.is_investigate(25, 9, 3))     # the "25 events" example vs a normal of 9
        self.assertFalse(forecast.is_investigate(11, 9, 3))    # slightly high: normal variation
        self.assertFalse(forecast.is_investigate(3, 1, 1))     # 3x but only 2 more events: not worth an operator
        self.assertTrue(forecast.is_investigate(12, 3, 1.7))   # 4x and +9

    def test_recommendation_text(self):
        t = local_hour(2026, 9, 16, 15)
        text = forecast.recommendation("investigate", 25, 9, 2.8, "Camera 01", FACE, t)
        self.assertIn("investigate", text)
        self.assertIn("15:00-16:00", text)
        self.assertIn("Camera 01", text)


class TrainPredictTest(TestCase):
    def setUp(self):
        self.end = local_hour(2026, 9, 20, 10).astimezone(dt_tz.utc)
        patcher = patch("boxapp.services.device_names", return_value={"1": "Camera 01", "2": "Camera 02"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cold_start_uses_seasonal_average_and_does_not_crash(self):
        for i in range(30):
            put(1, FACE, self.end - (30 - i) * H, 2)
        series = forecast.load_series()
        bundle = forecast.train(series, self.end)
        self.assertIsNone(bundle["model"])
        fcs = forecast.predict_next_hour(series, bundle, self.end)
        self.assertEqual(len(fcs), 1)
        self.assertEqual(fcs[0].model_used, "baseline")
        self.assertEqual(fcs[0].status, "normal")

    def test_under_a_day_of_history_makes_no_forecast(self):
        for i in range(10):
            put(1, FACE, self.end - (10 - i) * H, 2)
        series = forecast.load_series()
        self.assertEqual(forecast.predict_next_hour(series, forecast.train(series, self.end), self.end), [])

    def test_model_is_validated_on_unseen_hours_and_reacts_to_a_surge(self):
        seed_bursty(self.end)
        series = forecast.load_series()
        bundle = forecast.train(series, self.end)
        m = bundle["metrics"]
        self.assertIn("baseline", m["mae"])
        self.assertTrue(bundle["use_model"], m)               # bursts make the previous hour informative
        self.assertLess(m["mae_model"], m["mae_baseline"])    # a model is only used when it beats the plain average
        self.assertIn(m["used"], ("gbm", "glm"))

        def predicted_for_cam2(prev_hour_count):
            put(2, FACE, self.end - H, prev_hour_count)
            fc = {f.device_id: f for f in forecast.predict_next_hour(forecast.load_series(), bundle, self.end)}
            return fc[2].predicted

        results = [predicted_for_cam2(n) for n in (1, 6, 14, 40)]
        self.assertEqual(results, sorted(results))            # more events last hour never lowers the forecast
        self.assertGreater(results[-1], results[0] * 2)       # a surge clearly raises it

    def test_flagged_forecast_carries_a_recommendation(self):
        """Plumbing: a forecast of 25 against a normal of ~9 becomes an 'investigate' row with advice for the operator."""
        for i in range(24 * 10):
            put(1, FACE, self.end - (24 * 10 - i) * H, 9)       # steady 9 per hour
        series = forecast.load_series()

        class Stub:
            name = "gbm"
            def predict(self, xs):
                return np.array([25.0] * len(xs))

        (fc,) = forecast.predict_next_hour(series, {"model": Stub(), "use_model": True}, self.end)
        self.assertEqual((fc.predicted, fc.status, fc.model_used), (25.0, "investigate", "gbm"))
        self.assertAlmostEqual(fc.normal, 9.0, places=1)
        self.assertIn("Camera 01", fc.recommendation)
        self.assertIn("operator should investigate", fc.recommendation)

    def test_actuals_are_filled_after_the_hour(self):
        EventForecast.objects.create(target_hour=self.end - H, device_id=1, event_type=FACE, predicted=5, normal=4)
        put(1, FACE, self.end - H, 7)
        self.assertEqual(forecast.fill_actuals(self.end), 1)
        self.assertEqual(EventForecast.objects.get().actual, 7)


@override_settings(FORECAST_BACKFILL_DAYS=2)
class CollectorTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = mock_box.serve(0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        box.__init__(f"http://127.0.0.1:{cls.srv.server_address[1]}", "a", "b")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def expected(self, records, minors):
        c = Counter()
        for r in records:
            minor = r["additional"]["alarm_minor"]
            if minor in minors:
                t = datetime.fromtimestamp(int(r["global_info"]["time_ms"]) / 1000, tz=dt_tz.utc)
                c[(r["additional"]["device_id"], minor, forecast.floor_hour(t).astimezone(dt_tz.utc))] += 1
        return c

    def now(self):
        return datetime.fromtimestamp(mock_box.BASE_MS / 1000 + 3 * 3600, tz=dt_tz.utc)

    def stored(self, minors):
        return Counter({(r.device_id, r.event_type, r.hour_start): r.count
                        for r in HourlyEventCount.objects.filter(event_type__in=minors)})

    def test_recognition_events_are_bucketed_per_hour_camera_type_and_counted_once(self):
        records = mock_box.DATASETS["face_comparison_successful"] + mock_box.DATASETS["stranger"]
        n = forecast.sync_source("recognition", self.now())
        want = self.expected(records, {FACE, "stranger"})
        self.assertEqual(n, 50)
        self.assertEqual(self.stored({FACE, "stranger"}), want)
        # running again changes nothing (the mock ignores time filters and returns the same records for every window)
        forecast.sync_source("recognition", self.now() + H)
        self.assertEqual(self.stored({FACE, "stranger"}), want)

    def test_capture_events(self):
        records = mock_box.DATASETS["face_capture"] + mock_box.DATASETS["body_capture"]
        self.assertEqual(forecast.sync_source("capture", self.now()), 20)
        want = self.expected(records, {"face_capture", "body_capture"})
        self.assertEqual(self.stored({"face_capture", "body_capture"}), want)
        self.assertGreater(len({h for _, _, h in want}), 1)   # these records really span two clock hours

    def test_movement_alarm_events_are_counted(self):
        rule = MovementAlarmRule.objects.create(name="r", device_id=2, zone_points=[[0, 0], [1, 0], [1, 1]])
        when = self.now() - 2 * H
        for _ in range(3):
            ev = MovementAlarmEvent.objects.create(rule=rule, device_id=2)
            MovementAlarmEvent.objects.filter(pk=ev.pk).update(detected_at=when)
        self.assertEqual(forecast.sync_movement(self.now()), 3)
        row = HourlyEventCount.objects.get(event_type="movement")
        self.assertEqual((row.device_id, row.count), (2, 3))

    def test_full_cycle_with_history_in_place(self):
        end = forecast.floor_hour(self.now())
        seed_bursty(end.astimezone(dt_tz.utc), days=14)
        for name in ("recognition", "capture"):
            ForecastState.objects.create(key=f"sync:{name}", synced_until=end)   # box already synced: no overwrite
        with patch("boxapp.services.device_names", return_value={"1": "Camera 01", "2": "Camera 02"}):
            summary = forecast.run_cycle(self.now())
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["forecasts"], 2)
        self.assertIn("flagged", summary)
        self.assertEqual(ForecastState.objects.get(key="agent").data["forecasts"], 2)

    def test_cycle_survives_box_outage(self):
        with patch("boxapp.forecast.services._alarm_history", side_effect=RuntimeError("box down")):
            summary = forecast.run_cycle(self.now())
        self.assertTrue(any("recognition" in e for e in summary["errors"]))
        self.assertEqual(ForecastState.objects.get(key="sync:recognition").synced_until, None)   # retried next time


class PageTest(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("r", "r@x.com", "pw12345!"))
        end = local_hour(2026, 9, 20, 10).astimezone(dt_tz.utc)
        EventForecast.objects.create(target_hour=end, device_id=1, event_type=FACE, predicted=25, normal=9, normal_sd=3,
                                     ratio=2.8, status="investigate", recommendation="Expect about 25 events. An operator should investigate.")
        EventForecast.objects.create(target_hour=end - H, device_id=1, event_type=FACE, predicted=6, normal=5, actual=7)

    def test_page_lists_forecast_and_flags_it(self):
        with patch("boxapp.services.device_names", return_value={"1": "Camera 01"}):
            r = self.client.get("/admin/boxapp/eventforecastpage/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Camera 01")
        self.assertContains(r, "Investigate")
        self.assertContains(r, "1 to investigate")
        self.assertContains(r, "Past forecasts vs what happened")

    def test_camera_without_events_is_listed_with_a_reason(self):
        with patch("boxapp.services.device_names", return_value={"1": "Camera 01", "3": "Camera 03"}):
            r = self.client.get("/admin/boxapp/eventforecastpage/")
        self.assertContains(r, "Cameras without a forecast")
        self.assertContains(r, "Camera 03")
        self.assertContains(r, "No events recorded for this camera yet")

    def test_json_endpoint(self):
        body = self.client.get("/admin/forecast/data/").json()
        self.assertEqual(body["forecasts"][0]["next_hour_event_count"], 25)
        self.assertEqual(body["forecasts"][0]["status"], "investigate")

    def test_run_now_button_starts_the_agent_and_needs_permission(self):
        with patch("boxapp.forecast.ForecastAgent.run_async", return_value=True) as run:
            r = self.client.post("/admin/forecast/run/")
        self.assertEqual(r.status_code, 302)
        run.assert_called_once()
        self.assertEqual(self.client.get("/admin/forecast/run/").status_code, 405)   # POST only
        self.client.logout()
        self.client.force_login(get_user_model().objects.create_user("g", password="pw12345!", is_staff=True))
        self.assertEqual(self.client.post("/admin/forecast/run/").status_code, 403)
        self.assertEqual(self.client.get("/admin/forecast/data/").status_code, 403)


class MissingSklearnTest(TestCase):
    def test_agent_degrades_to_average_when_scikit_learn_is_missing(self):
        end = local_hour(2026, 9, 20, 10).astimezone(dt_tz.utc)
        seed_bursty(end)
        real_import = __import__

        def no_sklearn(name, *a, **k):
            if name.startswith("sklearn"):
                raise ModuleNotFoundError("No module named 'sklearn'")
            return real_import(name, *a, **k)

        with patch("builtins.__import__", side_effect=no_sklearn):
            bundle = forecast.train(forecast.load_series(), end)
        self.assertFalse(bundle["use_model"])
        self.assertIn("scikit-learn", bundle["metrics"]["note"])
