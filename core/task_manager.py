"""태스크 CRUD 및 핵심 비즈니스 로직."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import pytz
from sqlalchemy.orm import Session

from config import settings
from models.task import Priority, SubTask, Task, TaskStatus
from ai.task_breakdown import TaskBreakdownEngine
from core.capacity_planner import CapacityPlanner
from core.priority_engine import PriorityEngine


class TaskManager:
    def __init__(self, session: Session):
        self.session = session
        self.tz = pytz.timezone(settings.timezone)
        self.ai = TaskBreakdownEngine()
        self.planner = CapacityPlanner(session)
        self.priority_engine = PriorityEngine()

    # ─── 생성 ──────────────────────────────────────────────────────────────────

    def create_task(
        self,
        title: str,
        description: str = "",
        deadline: Optional[datetime] = None,
        priority: Optional[Priority] = None,
        project_id: Optional[int] = None,
        auto_breakdown: bool = True,
    ) -> Task:
        """
        새 태스크를 생성한다.
        auto_breakdown=True이면 Claude AI로 자동 분해 및 일정 배정.
        """
        # 기존 작업 요약 (AI 컨텍스트용)
        existing = self._existing_tasks_summary()

        task = Task(
            title=title,
            description=description,
            deadline=deadline,
            project_id=project_id,
            status=TaskStatus.PENDING,
        )

        if priority:
            task.priority = priority
        elif deadline:
            task.priority = self.priority_engine.suggest_priority(deadline)
        else:
            task.priority = Priority.MEDIUM

        self.session.add(task)
        self.session.flush()  # ID 확보

        if auto_breakdown and settings.anthropic_api_key:
            ai_result = self.ai.breakdown(
                title=title,
                description=description,
                deadline=deadline,
                available_hours_per_day=settings.daily_capacity_hours,
                existing_tasks_summary=existing,
            )
            self._apply_breakdown(task, ai_result)
        else:
            # AI 없으면 기본 1시간 단일 블록
            task.estimated_hours = 1.0

        # 우선순위 점수 계산
        task.priority_score = self.priority_engine.calculate_score(task)

        # 일정 배정
        blocks = self.planner.schedule_task(
            task,
            ai_daily_plan=getattr(task, "_ai_daily_plan", None),
        )

        self.session.flush()
        return task

    def _apply_breakdown(self, task: Task, ai_result: dict) -> None:
        """AI 분석 결과를 Task / SubTask에 적용."""
        task.estimated_hours = ai_result.get("total_estimated_hours", 1.0)
        if ai_result.get("recommended_priority"):
            task.priority = Priority(ai_result["recommended_priority"])

        for st_data in ai_result.get("subtasks", []):
            checklist_json = json.dumps(st_data.get("checklist", []), ensure_ascii=False)
            subtask = SubTask(
                task_id=task.id,
                title=st_data["title"],
                description=st_data.get("description", ""),
                order=st_data.get("order", 0),
                estimated_hours=st_data.get("estimated_hours", 0.5),
                checklist=checklist_json,
            )
            self.session.add(subtask)

        # daily_plan을 잠시 task에 붙여 schedule_task에서 참조
        task._ai_daily_plan = ai_result.get("daily_plan")
        # AI 분석 메타 저장 (고려 사항)
        task.description = (task.description or "") + "\n\n[AI 분석]\n" + "\n".join(
            ai_result.get("considerations", [])
        )

    # ─── 조회 ──────────────────────────────────────────────────────────────────

    def get_task(self, task_id: int) -> Optional[Task]:
        return self.session.get(Task, task_id)

    def list_tasks(
        self,
        status: Optional[TaskStatus] = None,
        priority: Optional[Priority] = None,
        project_id: Optional[int] = None,
        limit: int = 50,
    ) -> list[Task]:
        q = self.session.query(Task)
        if status:
            q = q.filter(Task.status == status)
        if priority:
            q = q.filter(Task.priority == priority)
        if project_id:
            q = q.filter(Task.project_id == project_id)
        tasks = q.limit(limit).all()
        return self.priority_engine.sort_tasks(tasks)

    def get_today_tasks(self) -> list[Task]:
        """오늘 배정된 블록이 있는 태스크 목록."""
        from models.task import ScheduleBlock
        today = datetime.now(self.tz).date()
        start = datetime(today.year, today.month, today.day, tzinfo=self.tz)
        end = start.replace(hour=23, minute=59, second=59)

        blocks = (
            self.session.query(ScheduleBlock)
            .filter(
                ScheduleBlock.start_time >= start,
                ScheduleBlock.start_time <= end,
                ScheduleBlock.status.in_([BlockStatus.SCHEDULED, BlockStatus.ACTIVE]),
            )
            .all()
        )
        task_ids = list({b.task_id for b in blocks})
        tasks = self.session.query(Task).filter(Task.id.in_(task_ids)).all()
        return self.priority_engine.sort_tasks(tasks)

    # ─── 업데이트 ───────────────────────────────────────────────────────────────

    def complete_task(self, task_id: int, actual_hours: Optional[float] = None) -> Task:
        task = self.session.get(Task, task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        task.status = TaskStatus.COMPLETED
        if actual_hours:
            task.actual_hours = actual_hours
        self.session.flush()
        return task

    def complete_subtask(self, subtask_id: int, actual_hours: Optional[float] = None) -> SubTask:
        st = self.session.get(SubTask, subtask_id)
        if not st:
            raise ValueError(f"SubTask {subtask_id} not found")
        st.status = TaskStatus.COMPLETED
        if actual_hours:
            st.actual_hours = actual_hours
        # 모든 서브태스크 완료 시 부모 태스크도 완료
        parent = st.task
        if all(s.status == TaskStatus.COMPLETED for s in parent.subtasks):
            parent.status = TaskStatus.COMPLETED
        self.session.flush()
        return st

    def update_priority(self, task_id: int, priority: Priority) -> Task:
        task = self.session.get(Task, task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        task.priority = priority
        task.priority_score = self.priority_engine.calculate_score(task)
        self.session.flush()
        return task

    # ─── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    def _existing_tasks_summary(self) -> str:
        tasks = self.session.query(Task).filter(
            Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])
        ).limit(10).all()
        if not tasks:
            return ""
        lines = []
        for t in tasks:
            dl = t.deadline.strftime("%Y-%m-%d") if t.deadline else "없음"
            lines.append(f"- {t.title} (마감: {dl}, 예상: {t.estimated_hours}h, 우선순위: {t.priority.value})")
        return "\n".join(lines)


# BlockStatus import fix
from models.task import BlockStatus  # noqa: E402
