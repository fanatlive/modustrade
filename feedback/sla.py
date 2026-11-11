"""Versioned eight-hour workday calendar used by the MVP (no invented holidays)."""
from __future__ import annotations
from datetime import date, datetime, timedelta, time
from typing import Any, Mapping
from zoneinfo import ZoneInfo
from django.conf import settings
from django.utils import timezone


def _zone(policy: Mapping[str, Any] | None = None) -> ZoneInfo:
    return ZoneInfo((policy or {}).get("timezone") or getattr(settings, "BUSINESS_TIMEZONE", "Asia/Bishkek"))


def policy_snapshot() -> dict[str, Any]:
    return {"version": 1, "timezone": str(_zone()), "days": "Mon-Fri", "weekdays": [0, 1, 2, 3, 4], "hours": "09:00-13:00,14:00-18:00", "holidays": [], "review_pause_hours": 40, "reopen_hours": 40, "no_response_grace_hours": 8, "priority_hours": {"HIGH": [4,24], "MEDIUM": [8,80], "LOW": [16,160]}}


def _segments(day: date, zone: ZoneInfo, policy: Mapping[str, Any] | None = None) -> list[tuple[datetime, datetime]]:
    snapshot = policy or {}
    if day.weekday() not in snapshot.get("weekdays", [0, 1, 2, 3, 4]) or day.isoformat() in snapshot.get("holidays", []):
        return []
    windows = [part.split("-", 1) for part in snapshot.get("hours", "09:00-13:00,14:00-18:00").split(",")]
    return [(datetime.combine(day, time.fromisoformat(begin), zone), datetime.combine(day, time.fromisoformat(end), zone)) for begin, end in windows]


def add_business_hours(dt: datetime, hours: float, *, policy: Mapping[str, Any] | None = None) -> datetime:
    if hours < 0:
        raise ValueError("Business hours cannot be negative")
    weekdays = (policy or {}).get("weekdays", [0, 1, 2, 3, 4])
    if not weekdays or any(not isinstance(day, int) or day not in range(7) for day in weekdays):
        raise ValueError("The saved business calendar has no valid working days")
    point = timezone.localtime(dt, _zone(policy))
    remaining = hours * 3600
    searched_days = 0
    while remaining > 0:
        for begin, end in _segments(point.date(), point.tzinfo, policy):
            if end <= begin:
                raise ValueError("Invalid working interval in the saved business calendar")
            point = max(point, begin)
            if point >= end:
                continue
            available = (end-point).total_seconds()
            if remaining <= available:
                return point + timedelta(seconds=remaining)
            remaining -= available
            point = end
        point = datetime.combine(point.date()+timedelta(days=1), time.min, point.tzinfo)
        searched_days += 1
        if searched_days > 36600:
            raise ValueError("Business calendar horizon exceeded")
    return point


def business_seconds(start: datetime, end: datetime, *, policy: Mapping[str, Any] | None = None) -> int:
    if end <= start:
        return 0
    start = timezone.localtime(start, _zone(policy))
    end = timezone.localtime(end, _zone(policy))
    day = start.date()
    total = 0.0
    while day <= end.date():
        for begin, finish in _segments(day, start.tzinfo, policy):
            total += max(0, (min(end,finish)-max(start,begin)).total_seconds())
        day += timedelta(days=1)
    return int(total)


def subtract_business_hours(dt: datetime, hours: float, *, policy: Mapping[str, Any] | None = None) -> datetime:
    """Schedule reminders backwards using the exact calendar saved with the cycle."""
    if hours < 0:
        raise ValueError("Business hours cannot be negative")
    weekdays = (policy or {}).get("weekdays", [0, 1, 2, 3, 4])
    if not weekdays or any(not isinstance(day, int) or day not in range(7) for day in weekdays):
        raise ValueError("The saved business calendar has no valid working days")
    point = timezone.localtime(dt, _zone(policy))
    remaining = hours * 3600
    searched_days = 0
    while remaining > 0:
        for begin, end in reversed(_segments(point.date(), point.tzinfo, policy)):
            if end <= begin:
                raise ValueError("Invalid working interval in the saved business calendar")
            point = min(point, end)
            if point <= begin:
                continue
            available = (point-begin).total_seconds()
            if remaining <= available:
                return point-timedelta(seconds=remaining)
            remaining -= available
            point = begin
        point = datetime.combine(point.date()-timedelta(days=1), time.max, point.tzinfo)
        searched_days += 1
        if searched_days > 36600:
            raise ValueError("Business calendar horizon exceeded")
    return point


def reopen_deadline(closed_at: datetime, *, policy: Mapping[str, Any] | None = None) -> datetime:
    return add_business_hours(closed_at, (policy or {}).get("reopen_hours", 40), policy=policy)


def initial_deadlines(dt: datetime, priority: str, *, policy: Mapping[str, Any] | None = None) -> tuple[datetime, datetime]:
    response, decision = (policy or {}).get("priority_hours", {"HIGH": (4,24), "MEDIUM":(8,80), "LOW":(16,160)})[priority]
    return add_business_hours(dt,response,policy=policy), add_business_hours(dt,decision,policy=policy)
