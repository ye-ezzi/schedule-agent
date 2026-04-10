"""Apple Calendar (iCloud CalDAV) 연동.

iCloud CalDAV를 통해 Apple Calendar에 이벤트를 동기화합니다.
App-specific password가 필요합니다: https://appleid.apple.com
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

import caldav
import pytz
from caldav.elements import dav, cdav
from icalendar import Calendar, Event, vText

from config import settings
from models.task import ScheduleBlock, Task

logger = logging.getLogger(__name__)


class AppleCalendarIntegration:
    """iCloud CalDAV를 통해 Apple Calendar에 이벤트를 씁니다."""

    def __init__(self):
        self.tz = pytz.timezone(settings.timezone)
        self._client = None
        self._calendar = None

    def _get_calendar(self) -> caldav.Calendar:
        """연결 및 캘린더 가져오기 (지연 초기화)."""
        if self._calendar:
            return self._calendar

        if not settings.apple_caldav_username or not settings.apple_caldav_password:
            raise ValueError(
                "Apple CalDAV 인증 정보가 없습니다. "
                "APPLE_CALDAV_USERNAME, APPLE_CALDAV_PASSWORD를 설정하세요."
            )

        self._client = caldav.DAVClient(
            url=settings.apple_caldav_url,
            username=settings.apple_caldav_username,
            password=settings.apple_caldav_password,
        )
        principal = self._client.principal()
        calendars = principal.calendars()

        # 이름으로 캘린더 찾기
        target_name = settings.apple_calendar_name
        for cal in calendars:
            cal_name = str(cal.name or "")
            if cal_name.lower() == target_name.lower():
                self._calendar = cal
                return self._calendar

        # 없으면 새로 생성
        logger.info(f"캘린더 '{target_name}' 없음, 생성합니다...")
        self._calendar = principal.make_calendar(name=target_name)
        return self._calendar

    # ─── 이벤트 생성/수정/삭제 ─────────────────────────────────────────────────

    def create_event(self, block: ScheduleBlock, task: Task) -> str:
        """ScheduleBlock → Apple Calendar 이벤트 생성. UID 반환."""
        uid = str(uuid.uuid4())
        ical_str = self._build_ical(block, task, uid)
        calendar = self._get_calendar()
        calendar.add_event(ical_str)
        logger.info(f"Apple Calendar event created: {uid}")
        return uid

    def update_event(self, block: ScheduleBlock, task: Task) -> None:
        """이벤트 업데이트."""
        if not block.apple_event_uid:
            return
        try:
            calendar = self._get_calendar()
            event = calendar.event_by_uid(block.apple_event_uid)
            new_ical = self._build_ical(block, task, block.apple_event_uid)
            event.data = new_ical
            event.save()
        except Exception as e:
            logger.error(f"Apple Calendar update failed: {e}")

    def delete_event(self, uid: str) -> None:
        """이벤트 삭제."""
        try:
            calendar = self._get_calendar()
            event = calendar.event_by_uid(uid)
            event.delete()
        except Exception as e:
            logger.error(f"Apple Calendar delete failed: {e}")

    def list_events(self, start: datetime, end: datetime) -> list:
        """기간 내 이벤트 조회."""
        try:
            calendar = self._get_calendar()
            events = calendar.date_search(start=start, end=end, expand=True)
            return events
        except Exception as e:
            logger.error(f"Apple Calendar list failed: {e}")
            return []

    # ─── iCal 빌더 ─────────────────────────────────────────────────────────────

    def _build_ical(self, block: ScheduleBlock, task: Task, uid: str) -> str:
        cal = Calendar()
        cal.add("prodid", "-//Schedule Agent//KR")
        cal.add("version", "2.0")
        cal.add("calscale", "GREGORIAN")

        event = Event()
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(self.tz))
        event.add("dtstart", block.start_time.astimezone(self.tz))
        event.add("dtend", block.end_time.astimezone(self.tz))

        priority_emoji = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}.get(
            task.priority.value, "📌"
        )
        event.add("summary", f"{priority_emoji} {task.title} ({block.planned_hours}h)")

        desc_parts = []
        if task.description:
            desc_parts.append(task.description[:500])
        if task.subtasks:
            remaining = [s.title for s in task.subtasks if s.status.value != "completed"]
            if remaining:
                desc_parts.append("남은 작업:\n" + "\n".join(f"• {t}" for t in remaining))
        if block.reschedule_count > 0:
            desc_parts.append(f"이월: {block.reschedule_count}회")

        event.add("description", "\n\n".join(desc_parts))
        event.add("categories", ["Schedule Agent"])

        # 우선순위 (iCal: 1=highest, 9=lowest)
        ical_priority = {"critical": 1, "high": 3, "medium": 5, "low": 8}.get(
            task.priority.value, 5
        )
        event.add("priority", ical_priority)

        # 알림 (VALARM) - 30분 전
        from icalendar import Alarm
        from datetime import timedelta
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", f"⏰ {task.title} 시작 30분 전")
        alarm.add("trigger", timedelta(minutes=-30))
        event.add_component(alarm)

        # 10분 전 알림
        alarm2 = Alarm()
        alarm2.add("action", "DISPLAY")
        alarm2.add("description", f"⚡ {task.title} 시작 10분 전")
        alarm2.add("trigger", timedelta(minutes=-10))
        event.add_component(alarm2)

        cal.add_component(event)
        return cal.to_ical().decode("utf-8")
