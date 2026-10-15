"""Regression tests for the live-video / routing fixes."""
import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

import mock_box
from boxapp import services
from boxapp.box import box

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None


class RtspUrlTest(TestCase):
    def test_credentials_added_when_url_has_none(self):
        url = services._with_credentials("rtsp://10.0.0.5:554/ISAPI/Streaming/channels/201", {"user": "admin", "password": "p@ss:w/rd"})
        self.assertEqual(url, "rtsp://admin:p%40ss%3Aw%2Frd@10.0.0.5:554/ISAPI/Streaming/channels/201")

    def test_raw_special_characters_in_password_are_encoded(self):
        url = services._with_credentials("rtsp://admin:p@ss#1@10.0.0.5:554/x", {})
        self.assertEqual(url, "rtsp://admin:p%40ss%231@10.0.0.5:554/x")

    def test_already_encoded_and_non_rtsp_left_alone(self):
        ok = "rtsp://admin:p%40ss@10.0.0.5/x"
        self.assertEqual(services._with_credentials(ok, {"user": "u", "password": "v"}), ok)
        self.assertEqual(services._with_credentials("/tmp/cam.mp4", {"user": "u", "password": "v"}), "/tmp/cam.mp4")

    def test_mask_url_hides_password(self):
        self.assertEqual(services.mask_url("rtsp://admin:secret@10.0.0.5:554/x"), "rtsp://***@10.0.0.5:554/x")
        self.assertNotIn("secret", services.mask_url("rtsp://admin:secret@10.0.0.5:554/x"))


class RoutesTest(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("r", "r@x.com", "pw12345!"))

    def test_movement_alarm_page_is_reachable(self):
        """It used to 404: the route was listed after admin.site.urls, whose catch-all swallowed it."""
        with patch("boxapp.views.devices", return_value=[{"device_id": 1, "device_name": "Front Door"}]):
            r = self.client.get("/admin/movement-alarm/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Front Door")

    def test_movement_alarm_page_survives_box_outage(self):
        with patch("boxapp.views.devices", side_effect=httpx.ConnectError("down")):
            r = self.client.get("/admin/movement-alarm/")
        self.assertEqual(r.status_code, 200)


@unittest.skipIf(cv2 is None, "opencv-python-headless not installed")
class LiveDiagnosticsTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.video = os.path.join(cls.tmp.name, "cam.mp4")
        vw = cv2.VideoWriter(cls.video, cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 360))
        for i in range(60):
            f = np.zeros((360, 640, 3), np.uint8)
            cv2.rectangle(f, (10 + i * 8, 100), (110 + i * 8, 200), (0, 255, 0), -1)
            vw.write(f)
        vw.release()
        mock_box.DEVICES[0]["rtsp_param"]["url"] = cls.video
        cls.srv = mock_box.serve(0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        box.__init__(f"http://127.0.0.1:{cls.srv.server_address[1]}", "a", "b")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.client.force_login(get_user_model().objects.create_superuser("r", "r@x.com", "pw12345!"))

    def test_stream_viewer_cap_returns_503_instead_of_freezing_the_portal(self):
        from boxapp import views

        with patch.object(views, "_stream_slots", threading.BoundedSemaphore(1)):
            first = self.client.get("/admin/live/1/stream/")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(self.client.get("/admin/live/1/stream/").status_code, 503)
            first.close()                                      # viewer leaves -> slot is free again
            again = self.client.get("/admin/live/1/stream/")
            self.assertEqual(again.status_code, 200)
            again.close()

    def test_status_endpoint_reports_state_without_leaking_url(self):
        self.assertEqual(self.client.get("/admin/live/1/snapshot/").status_code, 200)
        r = self.client.get("/admin/live/1/status/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn(body["state"], ("live", "connecting", "reconnecting", "idle"))
        self.assertNotIn("secret", r.content.decode())

    def test_check_live_command_names_the_failing_stage(self):
        out = io.StringIO()
        call_command("check_live", "1", "--seconds", "2", stdout=out)
        text = out.getvalue()
        self.assertIn("first frame after", text)
        self.assertIn("device 1: works", text)

        out = io.StringIO()
        call_command("check_live", "2", stdout=out)          # device 2 has no rtsp_param in the mock box
        self.assertIn("no RTSP URL", out.getvalue())


class BoxLoginErrorTest(TestCase):
    def test_non_json_login_reply_is_a_clean_box_error(self):
        """A wrong B4H_BASE_URL (an HTML page answers) used to raise a raw JSONDecodeError -> HTTP 500."""
        from boxapp.b4h import B4HClient, B4HError

        client = B4HClient("http://127.0.0.1:1", "a", "b")
        client._http = httpx.Client(base_url="http://box", transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>nginx</html>")))
        with self.assertRaises(B4HError) as cm:
            client.login()
        self.assertIn("B4H_BASE_URL", str(cm.exception))
