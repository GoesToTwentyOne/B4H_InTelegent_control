"""
Live video: one background thread per camera reads the RTSP stream with OpenCV and keeps the
latest frame as a JPEG. Any number of browser viewers share that one connection.

Why it is written defensively: a camera that is unreachable, slow to send its first keyframe, or
that stalls mid-stream must never block a thread forever or leave the browser with a silent blank
tile. Every failure is recorded in `status()` so the page (and `manage.py check_live`) can say why.
"""
import atexit
import logging
import os
import threading
import time

from django.conf import settings

# RTSP over TCP is far more reliable than UDP. settings.py also sets this; setdefault keeps either one.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402

from .services import mask_url  # noqa: E402

log = logging.getLogger("b4h.live")

IDLE_STOP = 10        # seconds with no viewer before the RTSP connection is closed
MAX_WIDTH = 960       # frames wider than this are downscaled
JPEG_QUALITY = 70
STALL_LIMIT = 15      # a stream that already worked is ended after this many silent seconds


def open_capture(url: str):
    """Open an RTSP/file source with open and read timeouts (so a dead camera fails in seconds)."""
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, settings.LIVE_OPEN_TIMEOUT_MS,
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, settings.LIVE_READ_TIMEOUT_MS,
    ]
    try:
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    except (TypeError, cv2.error):          # very old OpenCV without the params overload
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG)


class CameraStream:
    def __init__(self, url: str):
        self.url = url
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._jpeg: bytes | None = None
        self._seq = 0
        self._last_access = 0.0
        self._stop = False
        # diagnostics, shown by the live page and /admin/live/<id>/status/
        self.state = "idle"            # idle | connecting | live | reconnecting
        self.last_error = ""
        self.last_frame_at = 0.0
        self.frame_size = ""

    # ------------------------------------------------------------ worker thread
    def _ensure_running(self) -> None:
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                self._stop = False
                self._thread = threading.Thread(target=self._run, name="live-camera", daemon=True)
                self._thread.start()

    def _set_error(self, msg: str) -> None:
        self.last_error = msg
        log.warning("%s [%s]", msg, mask_url(self.url))

    def _run(self) -> None:
        cap, fails = None, 0
        self.state = "connecting"
        try:
            while not self._stop and time.time() - self._last_access < IDLE_STOP:
                if cap is None:
                    cap = open_capture(self.url)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        fails += 1
                        self.state = "reconnecting"
                        self._set_error(
                            "Cannot open the RTSP stream: wrong camera user/password, wrong URL, camera/NVR "
                            "unreachable from this PC, or too many RTSP connections on the camera. "
                            "Run: python manage.py check_live"
                        )
                        time.sleep(min(3 * fails, 10))
                        continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    cap.release()
                    cap = None
                    fails += 1
                    self.state = "reconnecting"
                    self._set_error("Connected, but no frames arrived (stream stalled or codec not decodable)")
                    time.sleep(min(fails, 5))
                    continue
                fails = 0
                h, w = frame.shape[:2]
                if w > MAX_WIDTH:
                    frame = cv2.resize(frame, (MAX_WIDTH, int(h * MAX_WIDTH / w)))
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if ok:
                    with self._cond:
                        self._jpeg = buf.tobytes()
                        self._seq += 1
                        self.last_frame_at = time.time()
                        self.frame_size = f"{frame.shape[1]}x{frame.shape[0]}"
                        self.state, self.last_error = "live", ""
                        self._cond.notify_all()
        except Exception as e:  # never let the thread die silently
            self._set_error(f"Stream thread crashed: {e!r}")
            log.exception("live stream thread crashed")
        finally:
            if cap is not None:
                cap.release()
            self.state = "idle"
            with self._cond:
                self._cond.notify_all()      # wake viewers so they notice the stream ended

    def stop(self) -> None:
        self._stop = True
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)

    # ------------------------------------------------------------ viewers
    def _touch(self) -> None:
        self._last_access = time.time()

    def frames(self):
        """
        Yield JPEG bytes as new frames arrive.
        Before the first frame we wait up to LIVE_FIRST_FRAME_WAIT seconds (an H.264/H.265 main stream can
        take a long time to deliver its first keyframe); afterwards a stall of STALL_LIMIT seconds ends it.
        """
        self._touch()
        self._ensure_running()
        seen, waited, got_any = 0, 0, False
        while True:
            with self._cond:
                got = self._cond.wait_for(lambda: self._seq != seen or self._stop, timeout=5)
                jpeg, seen = self._jpeg, self._seq
            self._touch()
            if self._stop:
                return
            if not got:
                waited += 5
                if waited >= (STALL_LIMIT if got_any else settings.LIVE_FIRST_FRAME_WAIT):
                    return
                if self._thread is None or not self._thread.is_alive():
                    self._ensure_running()
                continue
            got_any, waited = True, 0
            yield jpeg

    def snapshot(self, timeout: float = 12) -> bytes | None:
        """One fresh JPEG (never a stale one left over from an earlier session)."""
        self._touch()
        self._ensure_running()
        deadline = time.time() + timeout
        with self._cond:
            while time.time() < deadline:
                if self._jpeg is not None and time.time() - self.last_frame_at < 5:
                    return self._jpeg
                self._cond.wait(timeout=1)
                self._touch()
        return None

    def status(self) -> dict:
        age = round(time.time() - self.last_frame_at, 1) if self.last_frame_at else None
        return {"state": self.state, "error": self.last_error, "frame_age_s": age, "size": self.frame_size}


_streams: dict[int, CameraStream] = {}
_registry_lock = threading.Lock()


def get_stream(device_id: int, url: str) -> CameraStream:
    with _registry_lock:
        s = _streams.get(device_id)
        if s is None or s.url != url:
            if s is not None:
                s._stop = True            # URL changed: retire the old connection
            s = _streams[device_id] = CameraStream(url)
        return s


@atexit.register
def _stop_all() -> None:
    """Close camera connections cleanly when the server exits."""
    for s in list(_streams.values()):
        s._stop = True
    for s in list(_streams.values()):
        s.stop()
