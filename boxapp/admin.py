"""
One admin page per box feature. Each page is a ModelAdmin whose changelist is replaced by a view
that reads live from the box, so login, permissions and look & feel all come from Django admin.
"""
import logging

import httpx
from django.contrib import admin
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse

from . import services
from .b4h import B4HError
from .forms import CaptureFilterForm, LogFilterForm, RecognitionFilterForm
from .models import AlgorithmPackage, CaptureRecord, Device, EventForecastPage, LiveView, LogView, Person, RecognitionRecord

log = logging.getLogger("b4h")


def _page_number(request) -> int:
    try:
        return max(1, int(request.GET.get("page", 1)))
    except ValueError:
        return 1


def _pagination(request, total: int, page: int, size: int) -> dict:
    num_pages = max(1, -(-total // size))
    qs = request.GET.copy()
    qs.pop("page", None)
    return {
        "page": page,
        "num_pages": num_pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < num_pages,
        "prev": page - 1,
        "next": page + 1,
        "base_qs": qs.urlencode(),
    }


class BoxPageAdmin(admin.ModelAdmin):
    """Read-only page backed by the box. Subclasses implement build_context()."""

    page_template = ""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def build_context(self, request) -> dict:
        raise NotImplementedError

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_permission(request):
            raise PermissionDenied
        ctx = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": self.model._meta.verbose_name_plural,
        }
        try:
            ctx.update(self.build_context(request))
        except B4HError as e:
            log.warning("Box error: %s", e)
            ctx["error"] = f"Box error on {e.path}: {e.message} (code {e.code})"
        except httpx.TransportError as e:
            log.warning("Box unreachable: %r", e)
            ctx["error"] = f"B4H box unreachable ({type(e).__name__}). Check B4H_BASE_URL and the network."
        return TemplateResponse(request, self.page_template, ctx)


@admin.register(RecognitionRecord)
class RecognitionAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/recognition.html"

    def build_context(self, request):
        form = RecognitionFilterForm(request.GET or RecognitionFilterForm.defaults())
        ctx = {"form": form, "rows": []}
        if form.is_valid():
            cd, page = form.cleaned_data, _page_number(request)
            rows, total = services.fetch_recognition(
                cd["start"], cd["end"], cd["minor"], page, cd["size"], cd["hide_deleted"],
            )
            ctx.update(rows=rows, pager=_pagination(request, total, page, cd["size"]))
        return ctx


@admin.register(CaptureRecord)
class CaptureAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/captures.html"

    def build_context(self, request):
        form = CaptureFilterForm(request.GET or CaptureFilterForm.defaults())
        ctx = {"form": form, "rows": []}
        if form.is_valid():
            cd, page = form.cleaned_data, _page_number(request)
            rows, total = services.fetch_captures(cd["start"], cd["end"], cd["target_type"], page, cd["size"])
            ctx.update(rows=rows, pager=_pagination(request, total, page, cd["size"]))
        return ctx


@admin.register(Person)
class PersonAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/people.html"

    def build_context(self, request):
        q = request.GET.get("q", "").strip()
        people = services.people()
        if q:
            people = [p for p in people if q.lower() in p["name"].lower() or q in p["person_id"]]
        return {"people": people, "q": q}


@admin.register(Device)
class DeviceAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/devices.html"

    def build_context(self, request):
        return {"devices": services.devices()}


@admin.register(LiveView)
class LiveAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/live.html"

    def build_context(self, request):
        with_stream = services.camera_ids()
        cameras = [
            {"id": d["device_id"], "name": d["device_name"], "configured": d["device_id"] in with_stream}
            for d in services.devices()
        ]
        try:
            cols = min(4, max(1, int(request.GET.get("cols", 2))))
        except ValueError:
            cols = 2
        return {"cameras": cameras, "cols": cols}


@admin.register(EventForecastPage)
class EventForecastAdmin(BoxPageAdmin):
    """Reads the portal's own database (filled by the forecast agent), not the box."""
    page_template = "admin/boxapp/forecast.html"

    def build_context(self, request):
        from . import forecast
        from .models import EventForecast, ForecastState, HourlyEventCount

        names = services.device_names()
        latest = EventForecast.objects.order_by("-target_hour").values_list("target_hour", flat=True).first()
        current = list(EventForecast.objects.filter(target_hour=latest)) if latest else []
        current.sort(key=lambda f: (f.status != f.STATUS_INVESTIGATE, -f.ratio, f.device_id))
        recent = list(EventForecast.objects.filter(actual__isnull=False).order_by("-target_hour", "device_id")[:30])

        def row(f):
            return {"f": f, "device": names.get(str(f.device_id), f"Camera {f.device_id}"), "label": forecast.label(f.event_type)}

<<<<<<< Updated upstream
<<<<<<< HEAD
=======
>>>>>>> Stashed changes
        # Cameras the box lists but that have no forecast: say why instead of silently leaving them out.
        with_forecast = {f.device_id for f in current}
        without = []
        for dev_id, dev_name in sorted(names.items(), key=lambda kv: int(kv[0])):
            if int(dev_id) in with_forecast:
                continue
            last = HourlyEventCount.objects.filter(device_id=int(dev_id)).order_by("-hour_start").first()
            if last is None:
                reason = "No events recorded for this camera yet, so there is nothing to forecast."
            else:
                reason = (f"Last event in {timezone.localtime(last.hour_start):%Y-%m-%d %H:00}. A camera is forecast only if it "
                          f"had an event in the last {forecast.ACTIVE_WITHIN_DAYS} days and has at least a day of history.")
            without.append({"id": dev_id, "name": dev_name, "reason": reason})

        agent = ForecastState.objects.filter(key="agent").first()
        return {
            "without_forecast": without if latest else [],
<<<<<<< Updated upstream
=======
        agent = ForecastState.objects.filter(key="agent").first()
        return {
>>>>>>> fe6c3d54a78eb17946e8cbe14d946fe2085e6570
=======
>>>>>>> Stashed changes
            "latest_hour": latest,
            "forecasts": [row(f) for f in current],
            "recent": [row(f) for f in recent],
            "flagged": sum(f.status == f.STATUS_INVESTIGATE for f in current),
            "agent": agent.data if agent else None,
            "agent_updated": agent.updated_at if agent else None,
            "running": forecast.ForecastAgent.running,
            "history_rows": HourlyEventCount.objects.count(),
        }


@admin.register(LogView)
class LogAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/logs.html"

    def build_context(self, request):
        form = LogFilterForm(request.GET or LogFilterForm.defaults())
        ctx = {"form": form, "rows": []}
        if form.is_valid():
            cd, page = form.cleaned_data, _page_number(request)
            rows, total = services.fetch_logs(cd["start"], cd["end"], page, cd["size"])
            ctx.update(rows=rows, pager=_pagination(request, total, page, cd["size"]))
        return ctx


@admin.register(AlgorithmPackage)
class AlgorithmAdmin(BoxPageAdmin):
    page_template = "admin/boxapp/algorithms.html"

    def build_context(self, request):
        return {"algorithms": services.algorithms()}



from django.contrib import admin

from .models import MovementAlarmEvent, MovementAlarmRule


@admin.register(MovementAlarmRule)
class MovementAlarmRuleAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "device_id",
        "enabled",
        "sensitivity",
        "trigger_frames",
        "cooldown_seconds",
        "updated_at",
    )
    list_filter = ("enabled", "device_id")
    search_fields = ("name",)


# @admin.register(MovementAlarmEvent)
# class MovementAlarmEventAdmin(admin.ModelAdmin):
#     list_display = (
#         "detected_at",
#         "rule",
#         "device_id",
#         "detection_score",
#         "acknowledged",
#     )
#     list_filter = ("acknowledged", "device_id")
#     readonly_fields = (
#         "rule",
#         "device_id",
#         "detected_at",
#         "duration_seconds",
#         "detection_score",
#     )
# @admin.register(MovementAlarmEvent)
# class MovementAlarmEventAdmin(admin.ModelAdmin):
#     list_display = (
#         "id",
#         "device",
#         "rule",
#         "created_at",
#     )

#     fields = (
#         "device",
#         "rule",
#         "message",
#         "created_at",
#     )

@admin.register(MovementAlarmEvent)
class MovementAlarmEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "rule",
        "device_id",
        "detected_at",
        "duration_seconds",
        "detection_score",
        "acknowledged",
    )

    fields = (
        "rule",
        "device_id",
        "duration_seconds",
        "detection_score",
        "acknowledged",
    )