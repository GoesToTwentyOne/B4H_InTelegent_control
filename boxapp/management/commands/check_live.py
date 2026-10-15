"""
Find out exactly why live video does not play, stage by stage.

    python manage.py check_live              # every camera the box lists
    python manage.py check_live 1            # one device id
    python manage.py check_live 1 --seconds 15 --substream

Stages: box login -> RTSP URL on the box -> credentials in the URL -> TCP reachability of the camera
port -> RTSP open -> first frame -> frame rate. The first stage that fails is the cause.
"""
import importlib.metadata as md
import socket
import time
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand

from boxapp import services
from boxapp.b4h import B4HError
from boxapp.box import box

OK, BAD, WARN = "  [ OK ]", "  [FAIL]", "  [WARN]"


def _fourcc(cap, cv2) -> str:
    v = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
    return "".join(chr((v >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ") or "unknown"


class Command(BaseCommand):
    help = "Diagnose why live video does not play (box -> RTSP URL -> network -> decode)."

    def add_arguments(self, parser):
        parser.add_argument("device_id", nargs="?", type=int)
        parser.add_argument("--seconds", type=int, default=8, help="how long to read frames (default 8)")
        parser.add_argument("--substream", action="store_true", help="test the sub stream (/channels/NN02)")

    def line(self, tag, msg):
        self.stdout.write(f"{tag} {msg}")

    def handle(self, *args, **opts):
        self.stdout.write("== OpenCV ==")
        try:
            import cv2
        except ImportError:
            self.line(BAD, "opencv is not installed: pip install opencv-python-headless")
            return
        dists = [d for d in ("opencv-python", "opencv-contrib-python", "opencv-python-headless",
                             "opencv-contrib-python-headless") if _installed(d)]
        self.line(OK, f"cv2 {cv2.__version__}, packages: {', '.join(dists) or 'none found'}")
        if len(dists) > 1:
            self.line(WARN, "several OpenCV packages are installed; they overwrite each other's cv2 module and "
                            "can break RTSP. Fix: pip uninstall -y " + " ".join(dists) +
                            " && pip install opencv-python-headless")
        self.line(OK, f"OPENCV_FFMPEG_CAPTURE_OPTIONS = {__import__('os').environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS')}")

        self.stdout.write("== Box ==")
        try:
            devices = services.devices()
        except (B4HError, httpx.HTTPError) as e:
            self.line(BAD, f"cannot read devices from {settings.B4H_BASE_URL}: {e!r}")
            self.stdout.write("  Check B4H_BASE_URL / B4H_USER / B4H_PASS in .env (5 wrong passwords lock the box account).")
            return
        self.line(OK, f"box reachable, {len(devices)} device(s): " +
                  ", ".join(f"#{d['device_id']} {d['device_name']}" for d in devices))
        try:
            urls = services._rtsp_urls()
        except (B4HError, httpx.HTTPError) as e:
            self.line(BAD, f"device_config failed: {e!r}")
            return

        targets = [opts["device_id"]] if opts["device_id"] is not None else [d["device_id"] for d in devices]
        verdicts = {}
        for dev in targets:
            verdicts[dev] = self.check_camera(cv2, dev, urls.get(dev), opts)
        self.stdout.write("== Summary ==")
        for dev, v in verdicts.items():
            self.line(OK if v == "works" else BAD, f"device {dev}: {v}")

    def check_camera(self, cv2, dev, url, opts) -> str:
        self.stdout.write(f"== Device {dev} ==")
        if not url:
            self.line(BAD, "the box has no RTSP URL for this device (rtsp_param.url missing)")
            return "no RTSP URL on the box"
        if opts["substream"]:
            url = services.to_substream(url)
        shown = services.mask_url(url)
        self.line(OK, f"RTSP URL: {shown}")
        parts = urlsplit(url)
        local_file = parts.scheme not in ("rtsp", "rtsps", "rtmp", "http", "https")
        if not local_file:
            if "@" not in parts.netloc:
                self.line(WARN, "URL has no user:password; most cameras refuse anonymous RTSP")
            host, port = parts.hostname, parts.port or 554
            try:
                t0 = time.time()
                socket.create_connection((host, port), timeout=5).close()
                self.line(OK, f"TCP {host}:{port} reachable ({time.time() - t0:.2f}s)")
            except OSError as e:
                self.line(BAD, f"cannot reach {host}:{port} from this PC: {e}")
                self.stdout.write("  Different subnet/VLAN/firewall, camera offline, or wrong IP in the box's device config.")
                return f"network: {host}:{port} unreachable"

        t0 = time.time()
        from boxapp.streams import open_capture
        cap = open_capture(url)
        if not cap.isOpened():
            cap.release()
            self.line(BAD, f"RTSP open failed after {time.time() - t0:.1f}s")
            self.stdout.write("  Port is reachable, so: wrong user/password (401), wrong stream path, or the camera/NVR "
                              "is out of RTSP sessions (close other viewers). Try --substream.")
            return "RTSP open failed (credentials, path or session limit)"
        self.line(OK, f"RTSP opened in {time.time() - t0:.1f}s, codec {_fourcc(cap, cv2)}")

        t0, n, first = time.time(), 0, None
        while time.time() - t0 < opts["seconds"]:
            ok, frame = cap.read()
            if not ok:
                break
            n += 1
            if first is None:
                first = time.time() - t0
                self.line(OK, f"first frame after {first:.1f}s, size {frame.shape[1]}x{frame.shape[0]}")
        cap.release()
        if first is None:
            self.line(BAD, "opened but no frame decoded")
            self.stdout.write("  Usually H.265/H.264+ the OpenCV build cannot decode: set the camera/NVR sub stream to "
                              "H.264 and use --substream (LIVE_SUBSTREAM=1).")
            return "opened but no decodable frames"
        elapsed = max(time.time() - t0 - first, 0.001)
        fps = (n - 1) / elapsed
        self.line(OK if fps >= 5 else WARN, f"{n} frames, about {fps:.1f} fps")
        if first > settings.LIVE_FIRST_FRAME_WAIT - 5:
            self.line(WARN, "first frame is slow; use the sub stream (LIVE_SUBSTREAM=1) or shorten the camera's keyframe interval")
        return "works"


def _installed(name: str) -> bool:
    try:
        md.version(name)
        return True
    except md.PackageNotFoundError:
        return False
