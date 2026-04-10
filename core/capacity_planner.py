"""가용 시간 계산 및 합리적 일정 배정 엔진."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pytz
from sqlalchemy.orm import Session

from config import settings
from models.task import BlockStatus, CapacityLog, ScheduleBlock, SubTask, Task, TaskStatus


class CapacityPlanner:
    """날짜별 가용 시간을 관리하고 작업을 합리적으로 배정한다."""

    def __init__(self, session: Session):
        self.session = session
        self.tz = pytz.timezone(settings.timezone)

    # ─── 가용 시간 관리 ────────────────────────────────────────────────────────

    def get_or_create_capacity(self, target_date: date) -> CapacityLog:
        """날짜별 CapacityLog 조회 또는 생성."""
        dt = datetime(target_date.year, target_date.month, target_date.day,
                      tzinfo=self.tz)
        log = self.session.query(CapacityLog).filter(
            CapacityLog.date == dt
        ).first()

        if not log:
            # 주말이면 기본값 절반
            is_weekend = target_date.weekday() >= 5
            default_hours = settings.daily_capacity_hours / 2 if is_weekend else settings.daily_capacity_hours
            log = CapacityLog(
                date=dt,
                available_hours=default_hours,
                scheduled_hours=0.0,
            )
            self.session.add(log)
            self.session.flush()
        return log

    def set_daily_capacity(
        self,
        target_date: date,
        available_hours: float,
        note: str = "",
        is_holiday: bool = False,
    ) -> CapacityLog:
        """특정 날짜의 가용 시간을 수동으로 설정."""
        log = self.get_or_create_capacity(target_date)
        log.available_hours = available_hours
        log.is_holiday = is_holiday
        log.is_custom = True
        if note:
            log.note = note
        self.session.flush()
        return log

    def get_available_slots(self, days_ahead: int = 14) -> list[dict]:
        """향후 N일간 가용 시간 슬롯 반환."""
        today = datetime.now(self.tz).date()
        slots = []
        for offset in range(days_ahead):
            d = today + timedelta(days=offset)
            cap = self.get_or_create_capacity(d)
            if cap.available_hours > 0:
                slots.append({
                    "date": d.isoformat(),
                    "available_hours": cap.available_hours,
                    "scheduled_hours": cap.scheduled_hours,
                    "free_hours": round(cap.remaining_hours, 2),
                })
        return slots

    # ─── 일정 배정 ─────────────────────────────────────────────────────────────

    def schedule_task(
        self,
        task: Task,
        ai_daily_plan: Optional[list[dict]] = None,
        start_from: Optional[date] = None,
    ) -> list[ScheduleBlock]:
        """
        Task(또는 AI daily_plan)를 기반으로 ScheduleBlock을 생성하고 캘린더에 배정.

        ai_daily_plan: [{"day_offset": int, "suggested_hours": float, "subtask_titles": [...]}]
        """
        blocks: list[ScheduleBlock] = []
        today = datetime.now(self.tz).date()
        base_date = start_from or today

        if ai_daily_plan:
            subtask_map = {st.title: st for st in task.subtasks}
            for day_plan in ai_daily_plan:
                target_date = base_date + timedelta(days=day_plan["day_offset"])
                cap = self.get_or_create_capacity(target_date)

                if cap.remaining_hours <= 0:
                    # 여유 없으면 다음 날 찾기
                    target_date = self._find_next_free_day(target_date, day_plan["suggested_hours"])
                    if target_date is None:
                        continue
                    cap = self.get_or_create_capacity(target_date)

                hours = min(day_plan["suggested_hours"], cap.remaining_hours)
                start_dt = self._next_available_time(target_date, cap)
                end_dt = start_dt + timedelta(hours=hours)

                subtask = None
                if day_plan.get("subtask_titles"):
                    first_title = day_plan["subtask_titles"][0]
                    subtask = subtask_map.get(first_title)

                block = ScheduleBlock(
                    task_id=task.id,
                    subtask_id=subtask.id if subtask else None,
                    start_time=start_dt,
                    end_time=end_dt,
                    planned_hours=hours,
                    status=BlockStatus.SCHEDULED,
                )
                self.session.add(block)
                cap.scheduled_hours = round(cap.scheduled_hours + hours, 2)
                blocks.append(block)
        else:
            # AI 계획 없으면 단순 배정
            remaining = task.estimated_hours or 1.0
            current_date = base_date
            while remaining > 0:
                cap = self.get_or_create_capacity(current_date)
                if cap.remaining_hours <= 0 or cap.is_holiday:
                    current_date += timedelta(days=1)
                    continue
                allot = min(remaining, cap.remaining_hours, settings.daily_capacity_hours)
                start_dt = self._next_available_time(current_date, cap)
                end_dt = start_dt + timedelta(hours=allot)
                block = ScheduleBlock(
                    task_id=task.id,
                    start_time=start_dt,
                    end_time=end_dt,
                    planned_hours=allot,
                    status=BlockStatus.SCHEDULED,
                )
                self.session.add(block)
                cap.scheduled_hours = round(cap.scheduled_hours + allot, 2)
                blocks.append(block)
                remaining = round(remaining - allot, 2)
                current_date += timedelta(days=1)

        self.session.flush()
        return blocks

    def _next_available_time(self, target_date: date, cap: CapacityLog) -> datetime:
        """해당 날짜에서 이미 배정된 시간 이후 시작 시각을 반환."""
        start_hour = settings.work_start_hour + cap.scheduled_hours
        start_hour = min(start_hour, settings.work_end_hour - 1)
        return datetime(
            target_date.year, target_date.month, target_date.day,
            int(start_hour), int((start_hour % 1) * 60),
            tzinfo=self.tz,
        )

    def _find_next_free_day(self, from_date: date, needed_hours: float) -> Optional[date]:
        """필요 시간이 충족되는 가장 빠른 날짜를 찾는다."""
        d = from_date + timedelta(days=1)
        for _ in range(30):
            cap = self.get_or_create_capacity(d)
            if not cap.is_holiday and cap.remaining_hours >= needed_hours:
                return d
            d += timedelta(days=1)
        return None

    # ─── 부하 요약 ─────────────────────────────────────────────────────────────

    def workload_summary(self, days: int = 7) -> list[dict]:
        """향후 N일간 부하 요약 반환."""
        today = datetime.now(self.tz).date()
        summary = []
        for offset in range(days):
            d = today + timedelta(days=offset)
            cap = self.get_or_create_capacity(d)
            summary.append({
                "date": d.isoformat(),
                "available_hours": cap.available_hours,
                "scheduled_hours": cap.scheduled_hours,
                "free_hours": round(cap.remaining_hours, 2),
                "utilization_pct": cap.utilization_pct,
                "is_holiday": cap.is_holiday,
            })
        return summary

    def is_overloaded(self, target_date: date) -> bool:
        cap = self.get_or_create_capacity(target_date)
        return cap.scheduled_hours > cap.available_hours
