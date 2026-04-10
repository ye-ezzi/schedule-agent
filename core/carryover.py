"""미완료 태스크/블록을 다음 날로 이월하는 서비스."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pytz
from sqlalchemy.orm import Session

from config import settings
from models.task import BlockStatus, ScheduleBlock, Task, TaskStatus


class CarryoverService:
    """매일 자정에 실행되어 미완료 항목을 이월한다."""

    def __init__(self, session: Session):
        self.session = session
        self.tz = pytz.timezone(settings.timezone)

    def run_daily_carryover(self) -> dict:
        """
        오늘까지 MISSED 또는 SCHEDULED(지난) 블록들을 다음 가용 슬롯으로 이월.
        Returns: {"carried_over": int, "details": [...]}
        """
        now = datetime.now(self.tz)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 지나간 SCHEDULED / ACTIVE 블록 중 완료되지 않은 것
        missed_blocks = (
            self.session.query(ScheduleBlock)
            .filter(
                ScheduleBlock.end_time < now,
                ScheduleBlock.status.in_([BlockStatus.SCHEDULED, BlockStatus.ACTIVE]),
            )
            .all()
        )

        details = []
        carried = 0

        for block in missed_blocks:
            block.status = BlockStatus.MISSED
            task = block.task

            # 태스크 자체가 이미 완료됐으면 스킵
            if task.status == TaskStatus.COMPLETED:
                block.status = BlockStatus.RESCHEDULED
                continue

            # 남은 작업 시간 계산
            remaining_hours = self._remaining_hours(block)
            if remaining_hours <= 0:
                continue

            # 다음 가용 슬롯 찾기
            new_date = self._find_next_slot(now.date() + timedelta(days=1), remaining_hours)
            if new_date is None:
                details.append({
                    "task": task.title,
                    "original_time": block.start_time.isoformat(),
                    "status": "no_slot_found",
                })
                continue

            # 새 블록 생성
            new_start = datetime(
                new_date.year, new_date.month, new_date.day,
                settings.work_start_hour, 0, tzinfo=self.tz
            )
            new_end = new_start + timedelta(hours=remaining_hours)

            new_block = ScheduleBlock(
                task_id=task.id,
                subtask_id=block.subtask_id,
                start_time=new_start,
                end_time=new_end,
                planned_hours=remaining_hours,
                status=BlockStatus.SCHEDULED,
                reschedule_count=block.reschedule_count + 1,
                rescheduled_from=block.start_time,
            )
            self.session.add(new_block)

            # 태스크 이월 카운트 증가
            task.carry_over_count += 1
            if not task.original_deadline:
                task.original_deadline = task.deadline

            block.status = BlockStatus.RESCHEDULED
            carried += 1
            details.append({
                "task": task.title,
                "original_time": block.start_time.isoformat(),
                "new_time": new_start.isoformat(),
                "remaining_hours": remaining_hours,
                "carry_over_count": task.carry_over_count,
            })

        self.session.flush()
        return {"carried_over": carried, "details": details}

    def defer_block(
        self,
        block_id: int,
        to_date: Optional[date] = None,
        reason: str = "",
    ) -> ScheduleBlock:
        """
        사용자가 수동으로 특정 블록을 다음 날(또는 지정 날)로 이월.
        """
        block = self.session.get(ScheduleBlock, block_id)
        if not block:
            raise ValueError(f"Block {block_id} not found")

        now = datetime.now(self.tz)
        if to_date is None:
            to_date = now.date() + timedelta(days=1)

        remaining = self._remaining_hours(block)
        new_start = datetime(to_date.year, to_date.month, to_date.day,
                             settings.work_start_hour, 0, tzinfo=self.tz)
        new_end = new_start + timedelta(hours=remaining)

        new_block = ScheduleBlock(
            task_id=block.task_id,
            subtask_id=block.subtask_id,
            start_time=new_start,
            end_time=new_end,
            planned_hours=remaining,
            status=BlockStatus.SCHEDULED,
            reschedule_count=block.reschedule_count + 1,
            rescheduled_from=block.start_time,
            note=reason,
        )
        self.session.add(new_block)

        block.status = BlockStatus.RESCHEDULED
        block.task.carry_over_count += 1

        self.session.flush()
        return new_block

    def _remaining_hours(self, block: ScheduleBlock) -> float:
        task = block.task
        completed = sum(
            s.actual_hours or s.estimated_hours
            for s in task.subtasks
            if s.status == TaskStatus.COMPLETED
        )
        total = task.estimated_hours or block.planned_hours
        return max(0.0, round(total - completed, 2))

    def _find_next_slot(self, from_date: date, needed_hours: float) -> Optional[date]:
        """필요 시간을 수용 가능한 가장 빠른 날짜."""
        from core.capacity_planner import CapacityPlanner
        planner = CapacityPlanner(self.session)
        d = from_date
        for _ in range(30):
            cap = planner.get_or_create_capacity(d)
            if not cap.is_holiday and cap.remaining_hours >= needed_hours:
                return d
            d += timedelta(days=1)
        return None
