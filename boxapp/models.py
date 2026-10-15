"""
"Virtual" models: no database tables (managed = False). They exist only so each page shows up in
the admin sidebar and gets a "view" permission you can hand out per user or group.
"""
from django.db import models


class _BoxPage(models.Model):
    class Meta:
        abstract = True
        managed = False
        default_permissions = ("view",)


class RecognitionRecord(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "recognition record"
        verbose_name_plural = "Recognition"


class CaptureRecord(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "capture record"
        verbose_name_plural = "Captures"


class Person(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "person"
        verbose_name_plural = "People"


class Device(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "device"
        verbose_name_plural = "Devices"


class LiveView(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "live view"
        verbose_name_plural = "Live view"


class LogView(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "log"
        verbose_name_plural = "Logs"

class EventForecastPage(_BoxPage):
    """Sidebar entry + permission for the next-hour event forecast page."""
    class Meta(_BoxPage.Meta):
        verbose_name = "event forecast"
        verbose_name_plural = "Event forecast"


class AlgorithmPackage(_BoxPage):
    class Meta(_BoxPage.Meta):
        verbose_name = "algorithm package"
        verbose_name_plural = "Algorithms"
        permissions = [("upload_algorithmpackage", "Can upload algorithm package")]















class MovementAlarmRule(models.Model):
    """
    Persistent configuration for the portal-side movement alarm.

    This is intentionally a real Django model because rules must survive
    server restarts. Camera credentials and RTSP URLs are never stored here.
    """

    name = models.CharField(max_length=120)
    device_id = models.IntegerField()
    zone_points = models.JSONField(default=list)
    enabled = models.BooleanField(default=True)

    # Motion sensitivity: minimum changed-pixel ratio in the zone.
    sensitivity = models.FloatField(default=0.015)

    # Require N consecutive detections before raising an alarm.
    trigger_frames = models.PositiveIntegerField(default=3)

    # Do not raise another alarm until this many seconds have elapsed.
    cooldown_seconds = models.PositiveIntegerField(default=30)

    # Alarm is considered active for display purposes for this duration.
    alarm_duration_seconds = models.PositiveIntegerField(default=10)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} (camera {self.device_id})"


# class MovementAlarmEvent(models.Model):
#     rule = models.ForeignKey(
#         MovementAlarmRule,
#         on_delete=models.CASCADE,
#         related_name="events",
#     )
#     device_id = models.IntegerField()
#     detected_at = models.DateTimeField(auto_now_add=True)
#     duration_seconds = models.PositiveIntegerField(default=0)
#     detection_score = models.FloatField(default=0)
#     acknowledged = models.BooleanField(default=False)

#     class Meta:
#         ordering = ["-detected_at"]

#     def __str__(self):
#         return f"{self.rule.name} @ {self.detected_at:%Y-%m-%d %H:%M:%S}"

class MovementAlarmEvent(models.Model):
    rule = models.ForeignKey(
        MovementAlarmRule,
        on_delete=models.CASCADE,
        related_name="events",
    )

    device_id = models.IntegerField(
        null=False,
        blank=False,
    )

    detected_at = models.DateTimeField(auto_now_add=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    detection_score = models.FloatField(default=0)
    acknowledged = models.BooleanField(default=False)

    class Meta:
        ordering = ["-detected_at"]

    def __str__(self):
        return f"{self.rule.name} @ {self.detected_at:%Y-%m-%d %H:%M:%S}"




# ----------------------------------------------------------------------------------------------
# Next-hour event forecast (see boxapp/forecast.py)
# ----------------------------------------------------------------------------------------------

class HourlyEventCount(models.Model):
    """
    How many events of one type one camera produced in one clock hour. Zero-event hours are NOT stored
    (the forecast code fills them in), so a missing row means "0 events".
    """

    device_id = models.IntegerField()
    event_type = models.CharField(max_length=40)      # face_comparison_successful, stranger, face_capture, body_capture, movement
    hour_start = models.DateTimeField()               # start of the clock hour
    count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["device_id", "event_type", "hour_start"], name="uniq_hourly_series_hour"),
        ]
        indexes = [models.Index(fields=["event_type", "hour_start"])]

    def __str__(self):
        return f"{self.event_type} cam {self.device_id} {self.hour_start:%Y-%m-%d %H:00} = {self.count}"


class EventForecast(models.Model):
    """The agent's prediction of next_hour_event_count for one camera + event type."""

    STATUS_NORMAL, STATUS_INVESTIGATE = "normal", "investigate"

    created_at = models.DateTimeField(auto_now=True)
    target_hour = models.DateTimeField()              # the hour being predicted (start)
    device_id = models.IntegerField()
    event_type = models.CharField(max_length=40)
    predicted = models.FloatField()                   # expected events in the target hour
    normal = models.FloatField()                      # typical count for that hour of the week
    normal_sd = models.FloatField(default=1.0)
    ratio = models.FloatField(default=1.0)            # predicted / normal
    status = models.CharField(max_length=16, default=STATUS_NORMAL)
    model_used = models.CharField(max_length=16, default="baseline")   # "model" or "baseline"
    recommendation = models.TextField(blank=True)
    actual = models.PositiveIntegerField(null=True, blank=True)        # filled in once the hour is over

    class Meta:
        ordering = ["-target_hour", "device_id", "event_type"]
        constraints = [
            models.UniqueConstraint(fields=["target_hour", "device_id", "event_type"], name="uniq_forecast_per_hour_series"),
        ]

    def __str__(self):
        return f"{self.event_type} cam {self.device_id} {self.target_hour:%Y-%m-%d %H:00}: {self.predicted:.1f}"


class ForecastState(models.Model):
    """Small key/value table for the agent: per-source sync progress, model metrics, last run summary."""

    key = models.CharField(max_length=60, unique=True)
    synced_until = models.DateTimeField(null=True, blank=True)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key
