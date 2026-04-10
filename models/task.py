"""SQLAlchemy ORM models for schedule-agent."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─── Enums ────────────────────────────────────────────────────────────────────

class Priority(str, enum.Enum):
    CRITICAL = "critical"   # 즉시 처리 필요
    HIGH = "high"           # 오늘 안에
    MEDIUM = "medium"       # 이번 주 안에
    LOW = "low"             # 언제든


class TaskStatus(str, enum.Enum):
    PENDING = "pending"         # 시작 전
    IN_PROGRESS = "in_progress" # 진행 중
    COMPLETED = "completed"     # 완료
    DEFERRED = "deferred"       # 다음 날로 이월
    CANCELLED = "cancelled"     # 취소


class BlockStatus(str, enum.Enum):
    SCHEDULED = "scheduled"     # 예정
    ACTIVE = "active"           # 진행 중
    DONE = "done"               # 완료
    MISSED = "missed"           # 미완료 (3시간 알림 대상)
    RESCHEDULED = "rescheduled" # 재일정


# ─── Models ───────────────────────────────────────────────────────────────────

class Project(Base):
    """상위 프로젝트 (여러 Task 묶음)."""
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.MEDIUM)
    color: Mapped[Optional[str]] = mapped_column(String(7))  # hex color

    # External IDs
    notion_page_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="project", cascade="all, delete-orphan")


class Task(Base):
    """작업 단위. AI가 분석해서 SubTask로 분해된다."""
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # 일정
    deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float)   # AI 예측 소요 시간
    actual_hours: Mapped[Optional[float]] = mapped_column(Float)      # 실제 소요 시간

    # 상태 / 우선순위
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING, index=True)
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.MEDIUM, index=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)  # 계산된 점수 (높을수록 우선)

    # 이월
    carry_over_count: Mapped[int] = mapped_column(Integer, default=0)  # 몇 번 이월됐는지
    original_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # 관계
    project_id: Mapped[Optional[int]] = mapped_column(ForeignKey("projects.id"))
    project: Mapped[Optional["Project"]] = relationship("Project", back_populates="tasks")

    # External IDs
    notion_page_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    google_event_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    apple_event_uid: Mapped[Optional[str]] = mapped_column(String(200), index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    subtasks: Mapped[List["SubTask"]] = relationship("SubTask", back_populates="task", cascade="all, delete-orphan", order_by="SubTask.order")
    schedule_blocks: Mapped[List["ScheduleBlock"]] = relationship("ScheduleBlock", back_populates="task", cascade="all, delete-orphan")

    @property
    def completed_subtasks(self) -> int:
        return sum(1 for s in self.subtasks if s.status == TaskStatus.COMPLETED)

    @property
    def progress_pct(self) -> float:
        if not self.subtasks:
            return 100.0 if self.status == TaskStatus.COMPLETED else 0.0
        return round(self.completed_subtasks / len(self.subtasks) * 100, 1)


class SubTask(Base):
    """Task를 AI가 세분화한 단위 작업."""
    __tablename__ = "subtasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    task: Mapped["Task"] = relationship("Task", back_populates="subtasks")

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    order: Mapped[int] = mapped_column(Integer, default=0)

    estimated_hours: Mapped[float] = mapped_column(Float, default=0.5)
    actual_hours: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.PENDING)

    # 체크리스트 항목 (추가 세분화)
    checklist: Mapped[Optional[str]] = mapped_column(Text)  # JSON 직렬화된 리스트

    # External IDs
    notion_block_id: Mapped[Optional[str]] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class ScheduleBlock(Base):
    """캘린더에 배정된 실제 시간 블록."""
    __tablename__ = "schedule_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    task: Mapped["Task"] = relationship("Task", back_populates="schedule_blocks")

    subtask_id: Mapped[Optional[int]] = mapped_column(ForeignKey("subtasks.id"))

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_hours: Mapped[float] = mapped_column(Float, nullable=False)

    status: Mapped[BlockStatus] = mapped_column(Enum(BlockStatus), default=BlockStatus.SCHEDULED)
    note: Mapped[Optional[str]] = mapped_column(Text)

    # 알림 발송 여부
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # 이월/재일정 추적
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)
    rescheduled_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # External IDs
    google_event_id: Mapped[Optional[str]] = mapped_column(String(200))
    apple_event_uid: Mapped[Optional[str]] = mapped_column(String(200))
    notion_block_id: Mapped[Optional[str]] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class CapacityLog(Base):
    """날짜별 가용 시간 기록 (업무/개인 시간 설정)."""
    __tablename__ = "capacity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, unique=True, index=True)
    available_hours: Mapped[float] = mapped_column(Float, nullable=False)   # 총 가용 시간
    scheduled_hours: Mapped[float] = mapped_column(Float, default=0.0)      # 배정된 시간
    actual_hours: Mapped[Optional[float]] = mapped_column(Float)            # 실제 소요

    note: Mapped[Optional[str]] = mapped_column(Text)  # 휴가, 반차 등 메모
    is_holiday: Mapped[bool] = mapped_column(Boolean, default=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)  # 사용자가 직접 설정

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def remaining_hours(self) -> float:
        return max(0.0, self.available_hours - self.scheduled_hours)

    @property
    def utilization_pct(self) -> float:
        if self.available_hours == 0:
            return 100.0
        return round(self.scheduled_hours / self.available_hours * 100, 1)


class NotificationLog(Base):
    """발송된 알림 이력."""
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[Optional[int]] = mapped_column(ForeignKey("tasks.id"))
    block_id: Mapped[Optional[int]] = mapped_column(ForeignKey("schedule_blocks.id"))

    notification_type: Mapped[str] = mapped_column(String(50))  # missed | reminder | overdue | daily_summary
    message: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(50))            # desktop | slack | webhook

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # 사용자 응답 (알림에서 일정 변경 시)
    user_response: Mapped[Optional[str]] = mapped_column(Text)  # JSON
