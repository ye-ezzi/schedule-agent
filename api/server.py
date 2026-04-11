"""FastAPI REST API 서버.

엔드포인트 목록:
  POST /tasks                        - 태스크 생성 (AI 분해 포함)
  GET  /tasks                        - 태스크 목록
  GET  /tasks/{id}                   - 태스크 상세
  PATCH /tasks/{id}/complete         - 완료 처리
  PATCH /tasks/{id}/priority         - 우선순위 변경
  GET  /tasks/today                  - 오늘 태스크

  [MCP 동기화 엔드포인트]
  GET  /tasks/{id}/mcp-payload       - Claude가 MCP 호출에 쓸 페이로드 반환
  PATCH /tasks/{id}/external-ids     - MCP 호출 후 받은 외부 ID 저장
  POST /schedule/capacity-from-gcal  - gcal_find_my_free_time 결과로 CapacityLog 업데이트

  GET  /schedule/today         - 오늘 일정 블록
  GET  /schedule/week          - 이번 주 일정
  POST /schedule/blocks/{id}/reschedule  - 블록 재일정

  GET  /capacity               - 7일 가용 시간 요약
  POST /capacity/{date}        - 특정 날 가용 시간 설정

  GET  /workload               - 전체 부하 분석

  GET  /auth/google            - Google OAuth URL (SDK 폴백)
  GET  /auth/google/callback   - Google OAuth 콜백 (SDK 폴백)

  POST /notifications/{id}/ack - 알림 확인 처리
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pytz
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from db.database import get_db, init_db
from models.task import Priority, TaskStatus

app = FastAPI(
    title="Schedule Agent API",
    description="Notion/Google Calendar/Apple Calendar 통합 스케줄 관리 에이전트",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ─── Request/Response schemas ─────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str = ""
    deadline: Optional[datetime] = None
    priority: Optional[Priority] = None
    project_id: Optional[int] = None
    auto_breakdown: bool = True
    sync_notion: bool = False
    sync_google: bool = False
    sync_apple: bool = False


class UpdatePriorityRequest(BaseModel):
    priority: Priority


class CompleteTaskRequest(BaseModel):
    actual_hours: Optional[float] = None


class RescheduleRequest(BaseModel):
    to_date: Optional[date] = None
    reason: str = ""


class SetCapacityRequest(BaseModel):
    available_hours: float = Field(..., ge=0, le=24)
    note: str = ""
    is_holiday: bool = False


class AckNotificationRequest(BaseModel):
    response: Optional[dict] = None


# ─── MCP 관련 schemas ─────────────────────────────────────────────────────────

class GoogleEventIdItem(BaseModel):
    block_id: int
    event_id: str


class ExternalIdsRequest(BaseModel):
    """Claude가 MCP 호출 후 받은 외부 ID를 저장."""
    notion_page_id: Optional[str] = None
    google_event_ids: Optional[list[GoogleEventIdItem]] = None  # 블록별 이벤트 ID


class CapacityFromGcalRequest(BaseModel):
    """gcal_find_my_free_time 파싱 결과를 받아 CapacityLog에 반영."""
    free_slots: list[dict]  # [{"date": "YYYY-MM-DD", "free_hours": float}]


# ─── Tasks ────────────────────────────────────────────────────────────────────

@app.post("/tasks", status_code=201)
def create_task(req: CreateTaskRequest, db: Session = Depends(get_db)):
    from core.task_manager import TaskManager

    mgr = TaskManager(db)
    task = mgr.create_task(
        title=req.title,
        description=req.description,
        deadline=req.deadline,
        priority=req.priority,
        project_id=req.project_id,
        auto_breakdown=req.auto_breakdown,
    )
    db.commit()

    # 외부 동기화
    _sync_task(task, db, req.sync_notion, req.sync_google, req.sync_apple)

    return _task_response(task)


@app.get("/tasks")
def list_tasks(
    status: Optional[TaskStatus] = None,
    priority: Optional[Priority] = None,
    project_id: Optional[int] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    from core.task_manager import TaskManager

    mgr = TaskManager(db)
    tasks = mgr.list_tasks(status=status, priority=priority, project_id=project_id, limit=limit)
    return [_task_response(t) for t in tasks]


@app.get("/tasks/today")
def today_tasks(db: Session = Depends(get_db)):
    from core.task_manager import TaskManager

    mgr = TaskManager(db)
    tasks = mgr.get_today_tasks()
    return [_task_response(t) for t in tasks]


@app.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    from models.task import Task

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_response(task)


@app.patch("/tasks/{task_id}/complete")
def complete_task(task_id: int, req: CompleteTaskRequest, db: Session = Depends(get_db)):
    from core.task_manager import TaskManager

    mgr = TaskManager(db)
    try:
        task = mgr.complete_task(task_id, req.actual_hours)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()

    # Notion 동기화
    if task.notion_page_id and settings.notion_api_key:
        try:
            from integrations.notion_client import NotionIntegration
            NotionIntegration().complete_page(task)
        except Exception:
            pass

    return _task_response(task)


@app.patch("/tasks/{task_id}/priority")
def update_priority(task_id: int, req: UpdatePriorityRequest, db: Session = Depends(get_db)):
    from core.task_manager import TaskManager

    mgr = TaskManager(db)
    try:
        task = mgr.update_priority(task_id, req.priority)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()
    return _task_response(task)


# ─── Schedule ─────────────────────────────────────────────────────────────────

@app.get("/schedule/today")
def today_schedule(db: Session = Depends(get_db)):
    from models.task import ScheduleBlock

    tz = pytz.timezone(settings.timezone)
    today = datetime.now(tz).date()
    start = datetime(today.year, today.month, today.day, tzinfo=tz)
    end = start.replace(hour=23, minute=59, second=59)

    blocks = (
        db.query(ScheduleBlock)
        .filter(
            ScheduleBlock.start_time >= start,
            ScheduleBlock.start_time <= end,
        )
        .order_by(ScheduleBlock.start_time)
        .all()
    )
    return [_block_response(b) for b in blocks]


@app.get("/schedule/week")
def week_schedule(db: Session = Depends(get_db)):
    from datetime import timedelta
    from models.task import ScheduleBlock

    tz = pytz.timezone(settings.timezone)
    today = datetime.now(tz).date()
    start = datetime(today.year, today.month, today.day, tzinfo=tz)
    end = start + timedelta(days=7)

    blocks = (
        db.query(ScheduleBlock)
        .filter(
            ScheduleBlock.start_time >= start,
            ScheduleBlock.start_time < end,
        )
        .order_by(ScheduleBlock.start_time)
        .all()
    )
    return [_block_response(b) for b in blocks]


@app.post("/schedule/blocks/{block_id}/reschedule")
def reschedule_block(block_id: int, req: RescheduleRequest, db: Session = Depends(get_db)):
    from core.carryover import CarryoverService

    svc = CarryoverService(db)
    try:
        new_block = svc.defer_block(block_id, to_date=req.to_date, reason=req.reason)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    db.commit()
    return _block_response(new_block)


# ─── Capacity ─────────────────────────────────────────────────────────────────

@app.get("/capacity")
def get_capacity(days: int = 7, db: Session = Depends(get_db)):
    from core.capacity_planner import CapacityPlanner

    planner = CapacityPlanner(db)
    return planner.workload_summary(days=days)


@app.post("/capacity/{target_date}")
def set_capacity(target_date: date, req: SetCapacityRequest, db: Session = Depends(get_db)):
    from core.capacity_planner import CapacityPlanner

    planner = CapacityPlanner(db)
    log = planner.set_daily_capacity(
        target_date=target_date,
        available_hours=req.available_hours,
        note=req.note,
        is_holiday=req.is_holiday,
    )
    db.commit()
    return {
        "date": target_date.isoformat(),
        "available_hours": log.available_hours,
        "scheduled_hours": log.scheduled_hours,
        "is_holiday": log.is_holiday,
    }


# ─── Workload Analysis ────────────────────────────────────────────────────────

@app.get("/workload")
def analyze_workload(db: Session = Depends(get_db)):
    from models.task import Task
    from core.capacity_planner import CapacityPlanner
    from ai.task_breakdown import TaskBreakdownEngine

    if not settings.anthropic_api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    tasks = (
        db.query(Task)
        .filter(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS]))
        .limit(20)
        .all()
    )
    tasks_summary = [
        {
            "title": t.title,
            "priority": t.priority.value,
            "deadline": t.deadline.isoformat() if t.deadline else None,
            "estimated_hours": t.estimated_hours,
            "status": t.status.value,
        }
        for t in tasks
    ]

    planner = CapacityPlanner(db)
    capacity_summary = planner.workload_summary(days=7)

    engine = TaskBreakdownEngine()
    result = engine.analyze_workload(tasks_summary, capacity_summary)
    return result


# ─── MCP 동기화 ───────────────────────────────────────────────────────────────

@app.get("/tasks/{task_id}/mcp-payload")
def get_mcp_payload(task_id: int, db: Session = Depends(get_db)):
    """
    Claude가 MCP 도구를 호출할 때 사용할 페이로드를 반환한다.

    반환값:
      notion_payload  : notion-create-pages 의 pages[0] 항목
      notion_parent   : {"database_id": ...} 또는 {"page_id": ...}
      gcal_payloads   : [{"block_id", "event"}, ...]  gcal_create_event 의 event 파라미터
      gcal_calendar_id: Google Calendar ID
      already_synced  : {"notion": bool, "google": bool}
    """
    from models.task import Task, ScheduleBlock
    from integrations.mcp_helper import build_notion_page_payload, build_gcal_event_payload

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Notion 페이로드
    notion_payload = build_notion_page_payload(task)
    notion_parent: dict = {}
    if settings.notion_tasks_database_id:
        notion_parent = {"database_id": settings.notion_tasks_database_id, "type": "database_id"}
    elif settings.notion_parent_page_id:
        notion_parent = {"page_id": settings.notion_parent_page_id, "type": "page_id"}

    # Google Calendar 페이로드 (블록별)
    gcal_payloads = []
    for block in task.schedule_blocks:
        gcal_payloads.append({
            "block_id": block.id,
            "existing_event_id": block.google_event_id,
            "event": build_gcal_event_payload(block, task),
        })

    return {
        "task_id": task_id,
        "task_title": task.title,
        "notion_payload": notion_payload,
        "notion_parent": notion_parent,
        "gcal_payloads": gcal_payloads,
        "gcal_calendar_id": settings.google_calendar_id,
        "already_synced": {
            "notion": bool(task.notion_page_id),
            "google": any(b.google_event_id for b in task.schedule_blocks),
        },
    }


@app.patch("/tasks/{task_id}/external-ids")
def save_external_ids(task_id: int, req: ExternalIdsRequest, db: Session = Depends(get_db)):
    """
    Claude가 MCP 호출로 생성한 외부 ID를 DB에 저장한다.

    - notion_page_id  → Task.notion_page_id
    - google_event_ids[{block_id, event_id}] → ScheduleBlock.google_event_id
    """
    from models.task import Task, ScheduleBlock

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    updated: dict = {}

    if req.notion_page_id:
        task.notion_page_id = req.notion_page_id
        updated["notion_page_id"] = req.notion_page_id

    if req.google_event_ids:
        updated["google_event_ids"] = []
        for item in req.google_event_ids:
            block = db.get(ScheduleBlock, item.block_id)
            if block and block.task_id == task_id:
                block.google_event_id = item.event_id
                updated["google_event_ids"].append({"block_id": item.block_id, "event_id": item.event_id})

    db.commit()
    return {"task_id": task_id, "updated": updated}


@app.post("/schedule/capacity-from-gcal")
def sync_capacity_from_gcal(req: CapacityFromGcalRequest, db: Session = Depends(get_db)):
    """
    gcal_find_my_free_time 결과(parse_gcal_free_slots 파싱 후)를 받아
    CapacityLog를 업데이트한다.

    기존 수동 설정(is_custom=True)은 덮어쓰지 않는다.
    """
    from datetime import date as date_type
    from core.capacity_planner import CapacityPlanner

    planner = CapacityPlanner(db)
    results = []

    for slot in req.free_slots:
        try:
            d = date_type.fromisoformat(slot["date"])
            free_hours = float(slot["free_hours"])
        except (KeyError, ValueError):
            continue

        cap = planner.get_or_create_capacity(d)
        if cap.is_custom:
            # 사용자가 수동 설정한 날은 건드리지 않음
            results.append({"date": slot["date"], "action": "skipped_custom"})
            continue

        cap.available_hours = round(free_hours, 2)
        results.append({
            "date": slot["date"],
            "available_hours": cap.available_hours,
            "action": "updated",
        })

    db.commit()
    return {"synced": len([r for r in results if r["action"] == "updated"]), "details": results}


# ─── Google Auth ──────────────────────────────────────────────────────────────

@app.get("/auth/google")
def google_auth():
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")
    from integrations.google_calendar import GoogleCalendarIntegration
    gcal = GoogleCalendarIntegration()
    url = gcal.get_auth_url()
    return {"auth_url": url}


@app.get("/auth/google/callback")
def google_callback(code: str):
    from integrations.google_calendar import GoogleCalendarIntegration
    gcal = GoogleCalendarIntegration()
    gcal.handle_callback(code)
    return {"message": "Google Calendar 인증 완료!"}


# ─── Notifications ────────────────────────────────────────────────────────────

@app.post("/notifications/{notification_id}/ack")
def ack_notification(notification_id: int, req: AckNotificationRequest, db: Session = Depends(get_db)):
    from notifications.notifier import Notifier
    notifier = Notifier(db)
    notifier.acknowledge(notification_id, req.response)
    db.commit()
    return {"acknowledged": True}


@app.get("/notifications")
def list_notifications(limit: int = 20, db: Session = Depends(get_db)):
    from models.task import NotificationLog
    logs = db.query(NotificationLog).order_by(NotificationLog.sent_at.desc()).limit(limit).all()
    return [
        {
            "id": log.id,
            "type": log.notification_type,
            "message": log.message,
            "channel": log.channel,
            "sent_at": log.sent_at.isoformat(),
            "acknowledged": log.acknowledged,
        }
        for log in logs
    ]


# ─── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _task_response(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority.value,
        "priority_score": task.priority_score,
        "deadline": task.deadline.isoformat() if task.deadline else None,
        "estimated_hours": task.estimated_hours,
        "actual_hours": task.actual_hours,
        "progress_pct": task.progress_pct,
        "carry_over_count": task.carry_over_count,
        "subtasks": [
            {
                "id": s.id,
                "title": s.title,
                "estimated_hours": s.estimated_hours,
                "status": s.status.value,
                "order": s.order,
            }
            for s in task.subtasks
        ],
        "notion_page_id": task.notion_page_id,
        "google_event_id": task.google_event_id,
        "created_at": task.created_at.isoformat(),
    }


def _block_response(block) -> dict:
    tz = pytz.timezone(settings.timezone)
    return {
        "id": block.id,
        "task_id": block.task_id,
        "task_title": block.task.title if block.task else None,
        "task_priority": block.task.priority.value if block.task else None,
        "start_time": block.start_time.astimezone(tz).isoformat(),
        "end_time": block.end_time.astimezone(tz).isoformat(),
        "planned_hours": block.planned_hours,
        "status": block.status.value,
        "reschedule_count": block.reschedule_count,
        "notification_sent": block.notification_sent,
    }


def _sync_task(task, db, sync_notion: bool, sync_google: bool, sync_apple: bool):
    """태스크를 외부 서비스에 동기화."""
    if sync_notion and settings.notion_api_key and settings.notion_tasks_database_id:
        try:
            from integrations.notion_client import NotionIntegration
            notion = NotionIntegration()
            page_id = notion.create_page(task)
            task.notion_page_id = page_id
            db.flush()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Notion sync failed: {e}")

    if (sync_google or sync_apple) and task.schedule_blocks:
        for block in task.schedule_blocks:
            if sync_google and settings.google_client_id:
                try:
                    from integrations.google_calendar import GoogleCalendarIntegration
                    gcal = GoogleCalendarIntegration()
                    eid = gcal.create_event(block, task)
                    block.google_event_id = eid
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Google sync failed: {e}")

            if sync_apple and settings.apple_caldav_username:
                try:
                    from integrations.apple_calendar import AppleCalendarIntegration
                    acal = AppleCalendarIntegration()
                    uid = acal.create_event(block, task)
                    block.apple_event_uid = uid
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Apple sync failed: {e}")
        db.flush()
