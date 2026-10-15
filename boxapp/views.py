"""
Non-admin-page views. Every route is wrapped in admin.site.admin_view() in config/urls.py (staff login only).
"""
import mimetypes
import threading
from datetime import datetime

import httpx
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse, StreamingHttpResponse

from . import services
from .b4h import B4HError
from .box import box
from .streams import get_stream


# ---------------------------------------------------------------- image proxy
# The browser can't send the box's session cookie, so Django fetches the image for it.

def box_image(request):
    uri = request.GET.get("uri", "")
    if not uri or "://" in uri or ".." in uri:
        return HttpResponseBadRequest("Bad image uri")
    for path, params in (("/web/" + uri.lstrip("./"), None), ("/device_storage/get_image", {"image_uri": uri})):
        try:
            content, ctype = box.get_bytes(path, params)
        except httpx.HTTPStatusError:
            continue
        except httpx.TransportError:
            return HttpResponse("B4H box unreachable", status=503)
        if ctype == "application/octet-stream":  # the box doesn't label its JPEGs
            ctype = mimetypes.guess_type(uri)[0] or ctype
        resp = HttpResponse(content, content_type=ctype)
        resp["Cache-Control"] = "private, max-age=86400"
        return resp
    raise Http404("Image not found on the box")


# ---------------------------------------------------------------- live video


def _camera(request, device_id: int):
    if not request.user.has_perm("boxapp.view_liveview"):
        raise PermissionDenied
    try:
        url = services.camera_rtsp_url(device_id)
    except (B4HError, httpx.HTTPError) as e:
        raise Http404(f"B4H box not reachable: {e}")
    if not url:
        raise Http404("The box has no RTSP URL for this camera")
    return get_stream(device_id, url)


_stream_slots = threading.BoundedSemaphore(settings.LIVE_MAX_STREAMS)


class _Slot:
    """Releases one live-stream slot, exactly once, however the response ends."""

    def __init__(self):
        self._lock, self._held = threading.Lock(), True

    def release(self):
        with self._lock:
            if self._held:
                self._held = False
                _stream_slots.release()


def live_stream(request, device_id: int):
    """MJPEG stream: works in a plain <img> tag, no JavaScript."""
    stream = _camera(request, device_id)
    # Each open stream occupies a server thread; refuse politely instead of freezing the whole portal.
    if not _stream_slots.acquire(blocking=False):
        return HttpResponse(f"Too many live viewers open (limit {settings.LIVE_MAX_STREAMS}). Close another live tab.",
                            status=503, headers={"Retry-After": "5"})
    slot = _Slot()

    def gen():
        try:
            for jpeg in stream.frames():
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg) + jpeg + b"\r\n"
        finally:
            slot.release()

    resp = StreamingHttpResponse(gen(), content_type="multipart/x-mixed-replace; boundary=frame")
    resp["Cache-Control"] = "no-store"
    close = resp.close

    def close_and_release():          # the generator may never have started (client gone): still free the slot
        slot.release()
        close()

    resp.close = close_and_release
    return resp


def live_snapshot(request, device_id: int):
    jpeg = _camera(request, device_id).snapshot()
    if not jpeg:
        return HttpResponse("Camera not responding", status=504)
    return HttpResponse(jpeg, content_type="image/jpeg")


def live_status(request, device_id: int):
    """Why a tile is blank: connection state + last error. Never contains the camera URL or password."""
    return JsonResponse(_camera(request, device_id).status())


# ---------------------------------------------------------------- box log file


def log_file_download(request):
    if not request.user.has_perm("boxapp.view_logview"):
        raise PermissionDenied
    try:
        content, ctype = services.log_file()
    except httpx.HTTPStatusError as e:
        return HttpResponse(f"Box answered HTTP {e.response.status_code} for the log file", status=502)
    except (B4HError, httpx.TransportError) as e:
        return HttpResponse(f"B4H box unreachable: {e}", status=503)
    ext = "zip" if "zip" in ctype else "json" if "json" in ctype else "txt"
    resp = HttpResponse(content, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="b4h-log-{datetime.now():%Y%m%d-%H%M%S}.{ext}"'
    return resp














#-------------------------------------------------


from django.contrib import admin, messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import MovementAlarmEvent,MovementAlarmRule
from .movement_alarm import MovementAlarmManager
from .services import devices


@staff_member_required
def movement_alarm_page(request):
    if not request.user.has_perm("boxapp.view_movementalarmrule"):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    rules = list(MovementAlarmRule.objects.all())
    events = MovementAlarmEvent.objects.select_related("rule")[:100]
    try:
        camera_list = devices()
    except (B4HError, httpx.HTTPError) as e:        # box down: still show rules and events
        messages.error(request, f"Could not read the camera list from the box: {e}")
        camera_list = []

    return render(
        request,
        "admin/boxapp/movement_alarm.html",
        {
            **admin.site.each_context(request),      # sidebar + breadcrumbs of the admin base template
            "opts": MovementAlarmRule._meta,
            "title": "Movement alarm",
            "rules": rules,
            "events": events,
            "devices": camera_list,
        },
    )


@staff_member_required
@require_http_methods(["POST"])
def movement_alarm_create(request):
    if not request.user.has_perm("boxapp.add_movementalarmrule"):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    import json

    try:
        payload = json.loads(request.body or "{}")
        name = str(payload["name"]).strip()
        device_id = int(payload["device_id"])
        points = payload["zone_points"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid request"}, status=400)

    if not name or not isinstance(points, list) or len(points) < 3:
        return JsonResponse(
            {"error": "Name and at least 3 zone points are required"},
            status=400,
        )

    rule = MovementAlarmRule.objects.create(
        name=name,
        device_id=device_id,
        zone_points=points,
        sensitivity=float(payload.get("sensitivity", 0.015)),
        trigger_frames=int(payload.get("trigger_frames", 3)),
        cooldown_seconds=int(payload.get("cooldown_seconds", 30)),
        alarm_duration_seconds=int(payload.get("alarm_duration_seconds", 10)),
    )

    MovementAlarmManager.start_rule(rule.id)
    return JsonResponse({"ok": True, "id": rule.id})


@staff_member_required
@require_http_methods(["POST"])
def movement_alarm_toggle(request, rule_id):
    rule = get_object_or_404(MovementAlarmRule, pk=rule_id)

    if not request.user.has_perm("boxapp.change_movementalarmrule"):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    rule.enabled = not rule.enabled
    rule.save(update_fields=["enabled", "updated_at"])

    if rule.enabled:
        MovementAlarmManager.start_rule(rule.id)
    else:
        MovementAlarmManager.stop_rule(rule.id)

    messages.success(
        request,
        f"{rule.name}: {'enabled' if rule.enabled else 'disabled'}",
    )
    return redirect("movement_alarm")


@staff_member_required
@require_http_methods(["POST"])
def movement_alarm_delete(request, rule_id):
    rule = get_object_or_404(MovementAlarmRule, pk=rule_id)

    if not request.user.has_perm("boxapp.delete_movementalarmrule"):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    MovementAlarmManager.stop_rule(rule.id)
    rule.delete()
    messages.success(request, "Movement alarm rule deleted.")
    return redirect("movement_alarm")


@staff_member_required
@require_http_methods(["POST"])
def movement_alarm_ack(request, event_id):
    event = get_object_or_404(MovementAlarmEvent, pk=event_id)

    if not request.user.has_perm("boxapp.change_movementalarmevent"):
        return JsonResponse({"detail": "Forbidden"}, status=403)

    event.acknowledged = True
    event.save(update_fields=["acknowledged"])
    return redirect("movement_alarm")



# ---------------------------------------------------------------- next-hour event forecast

@require_http_methods(["POST"])
def forecast_run(request):
    """'Run now' button: start one collect -> train -> predict cycle in the background."""
    from .forecast import ForecastAgent

    if not request.user.has_perm("boxapp.view_eventforecastpage"):
        raise PermissionDenied
    if ForecastAgent.run_async():
        messages.success(request, "Forecast agent started. Reload this page in a minute.")
    else:
        messages.info(request, "The forecast agent is already running.")
    return redirect("admin:boxapp_eventforecastpage_changelist")


def forecast_data(request):
    """The latest forecasts as JSON (for other tools or a dashboard)."""
    from .forecast import label
    from .models import EventForecast

    if not request.user.has_perm("boxapp.view_eventforecastpage"):
        raise PermissionDenied
    latest = EventForecast.objects.order_by("-target_hour").values_list("target_hour", flat=True).first()
    rows = EventForecast.objects.filter(target_hour=latest) if latest else []
    return JsonResponse({
        "target_hour": latest.isoformat() if latest else None,
        "forecasts": [{
            "device_id": f.device_id, "event_type": f.event_type, "event_label": label(f.event_type),
            "next_hour_event_count": f.predicted, "normal": f.normal, "ratio": f.ratio,
            "status": f.status, "model_used": f.model_used, "recommendation": f.recommendation,
        } for f in rows],
    })
