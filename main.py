#!/usr/bin/env python3
"""Schedule Agent CLI.

사용법:
  python main.py add "프로젝트 제안서 작성" --deadline 2026-04-17 --priority high
  python main.py list
  python main.py today
  python main.py complete 1
  python main.py schedule
  python main.py capacity
  python main.py serve
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import pytz
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich import box

from config import settings
from db.database import init_db, get_session
from models.task import Priority, TaskStatus

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = typer.Typer(
    name="schedule-agent",
    help="Notion / Google Calendar / Apple Calendar 통합 스케줄 관리 에이전트",
    rich_markup_mode="rich",
)
console = Console()
tz = pytz.timezone(settings.timezone)


# ─── 태스크 명령어 ─────────────────────────────────────────────────────────────

@app.command("add")
def add_task(
    title: str = typer.Argument(..., help="태스크 제목"),
    description: str = typer.Option("", "-d", "--description", help="상세 설명"),
    deadline: Optional[str] = typer.Option(None, "--deadline", help="마감일 (YYYY-MM-DD 또는 YYYY-MM-DD HH:MM)"),
    priority: Optional[str] = typer.Option(None, "-p", "--priority", help="critical|high|medium|low"),
    no_ai: bool = typer.Option(False, "--no-ai", help="AI 분해 비활성화"),
    notion: bool = typer.Option(False, "--notion", help="Notion 동기화"),
    google: bool = typer.Option(False, "--google", help="Google Calendar 동기화"),
    apple: bool = typer.Option(False, "--apple", help="Apple Calendar 동기화"),
):
    """새 태스크를 추가하고 AI로 자동 분해합니다."""
    init_db()

    dl: Optional[datetime] = None
    if deadline:
        try:
            if ":" in deadline:
                dl = datetime.strptime(deadline, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            else:
                dl = datetime.strptime(deadline, "%Y-%m-%d").replace(
                    hour=23, minute=59, tzinfo=tz
                )
        except ValueError:
            console.print(f"[red]날짜 형식 오류: {deadline}[/red]")
            raise typer.Exit(1)

    pri: Optional[Priority] = None
    if priority:
        try:
            pri = Priority(priority.lower())
        except ValueError:
            console.print(f"[red]우선순위 값 오류: {priority}[/red]")
            raise typer.Exit(1)

    with get_session() as session:
        from core.task_manager import TaskManager

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                description="[cyan]AI로 태스크 분석 중..." if (not no_ai and settings.anthropic_api_key) else "[cyan]태스크 생성 중...",
                total=None,
            )

            mgr = TaskManager(session)
            task = mgr.create_task(
                title=title,
                description=description,
                deadline=dl,
                priority=pri,
                auto_breakdown=(not no_ai),
            )
            session.flush()

            # 외부 동기화
            if notion or google or apple:
                progress.update(task_id, description="[cyan]외부 서비스 동기화 중...")
                from api.server import _sync_task
                _sync_task(task, session, notion, google, apple)

            progress.update(task_id, completed=1)

    console.print()
    _print_task_panel(task)


@app.command("list")
def list_tasks(
    status: Optional[str] = typer.Option(None, "-s", "--status", help="pending|in_progress|completed|deferred"),
    priority: Optional[str] = typer.Option(None, "-p", "--priority"),
    limit: int = typer.Option(20, "-n", "--limit"),
):
    """태스크 목록 조회."""
    init_db()

    st: Optional[TaskStatus] = None
    pri: Optional[Priority] = None
    if status:
        try:
            st = TaskStatus(status)
        except ValueError:
            pass
    if priority:
        try:
            pri = Priority(priority)
        except ValueError:
            pass

    with get_session() as session:
        from core.task_manager import TaskManager
        mgr = TaskManager(session)
        tasks = mgr.list_tasks(status=st, priority=pri, limit=limit)

    if not tasks:
        console.print("[yellow]태스크가 없습니다.[/yellow]")
        return

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan")
    table.add_column("ID", style="dim", width=5)
    table.add_column("우선순위", width=10)
    table.add_column("제목", min_width=25)
    table.add_column("마감일", width=12)
    table.add_column("진행률", width=10)
    table.add_column("상태", width=12)
    table.add_column("이월", width=5)

    for t in tasks:
        priority_str = _priority_label(t.priority)
        status_str = _status_label(t.status)
        dl_str = t.deadline.astimezone(tz).strftime("%m/%d %H:%M") if t.deadline else "-"
        progress_str = f"{t.progress_pct}%"
        table.add_row(
            str(t.id),
            priority_str,
            t.title[:40],
            dl_str,
            progress_str,
            status_str,
            str(t.carry_over_count) if t.carry_over_count else "-",
        )

    console.print(table)


@app.command("today")
def today_tasks():
    """오늘 할 일 목록."""
    init_db()
    with get_session() as session:
        from models.task import ScheduleBlock, BlockStatus
        today = datetime.now(tz).date()
        start = datetime(today.year, today.month, today.day, tzinfo=tz)
        end = start.replace(hour=23, minute=59, second=59)

        blocks = (
            session.query(ScheduleBlock)
            .filter(
                ScheduleBlock.start_time >= start,
                ScheduleBlock.start_time <= end,
            )
            .order_by(ScheduleBlock.start_time)
            .all()
        )

    if not blocks:
        console.print(Panel("[yellow]오늘 예정된 작업이 없습니다[/yellow]", title="📅 오늘 일정"))
        return

    table = Table(box=box.SIMPLE_HEAVY, title=f"📅 오늘 일정 ({today.strftime('%Y/%m/%d')})")
    table.add_column("시간", style="cyan", width=14)
    table.add_column("작업", min_width=25)
    table.add_column("시간(h)", width=8)
    table.add_column("상태", width=12)

    total_hours = 0.0
    for b in blocks:
        time_str = (
            f"{b.start_time.astimezone(tz).strftime('%H:%M')} - "
            f"{b.end_time.astimezone(tz).strftime('%H:%M')}"
        )
        status_str = _block_status_label(b.status)
        total_hours += b.planned_hours
        table.add_row(
            time_str,
            b.task.title[:35] if b.task else "?",
            str(b.planned_hours),
            status_str,
        )

    console.print(table)
    console.print(f"[bold]총 예정 시간: {total_hours}h[/bold]")


@app.command("complete")
def complete_task(
    task_id: int = typer.Argument(..., help="완료할 태스크 ID"),
    actual_hours: Optional[float] = typer.Option(None, "-h", "--hours", help="실제 소요 시간"),
):
    """태스크를 완료 처리합니다."""
    init_db()
    with get_session() as session:
        from core.task_manager import TaskManager
        mgr = TaskManager(session)
        try:
            task = mgr.complete_task(task_id, actual_hours)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    console.print(f"[green]✅ 완료: {task.title}[/green]")


@app.command("schedule")
def show_schedule(days: int = typer.Option(7, "-d", "--days", help="조회할 일수")):
    """이번 주 일정 블록 조회."""
    init_db()
    from datetime import timedelta
    from models.task import ScheduleBlock

    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)

    with get_session() as session:
        blocks = (
            session.query(ScheduleBlock)
            .filter(
                ScheduleBlock.start_time >= start,
                ScheduleBlock.start_time < end,
            )
            .order_by(ScheduleBlock.start_time)
            .all()
        )

    if not blocks:
        console.print("[yellow]예정된 일정이 없습니다.[/yellow]")
        return

    current_day = None
    table = None
    tables = []

    for b in blocks:
        d = b.start_time.astimezone(tz).date()
        if d != current_day:
            if table:
                tables.append((current_day, table))
            current_day = d
            table = Table(
                box=box.SIMPLE,
                title=f"[bold]{d.strftime('%Y/%m/%d (%a)')}[/bold]",
                show_header=False,
            )
            table.add_column("시간", style="cyan", width=14)
            table.add_column("작업", min_width=30)
            table.add_column("h", width=5)
            table.add_column("상태", width=12)
        time_str = (
            f"{b.start_time.astimezone(tz).strftime('%H:%M')}-"
            f"{b.end_time.astimezone(tz).strftime('%H:%M')}"
        )
        table.add_row(
            time_str,
            (b.task.title[:35] if b.task else "?") + (f" ({'↩️ ' * b.reschedule_count}" if b.reschedule_count else ""),
            str(b.planned_hours),
            _block_status_label(b.status),
        )

    if table:
        tables.append((current_day, table))

    for d, t in tables:
        console.print(t)


@app.command("capacity")
def show_capacity(days: int = typer.Option(7, "-d", "--days")):
    """가용 시간 및 부하 현황."""
    init_db()
    with get_session() as session:
        from core.capacity_planner import CapacityPlanner
        planner = CapacityPlanner(session)
        summary = planner.workload_summary(days=days)

    table = Table(box=box.ROUNDED, title="📊 가용 시간 현황")
    table.add_column("날짜", style="bold")
    table.add_column("가용(h)", justify="right")
    table.add_column("배정(h)", justify="right")
    table.add_column("여유(h)", justify="right", style="green")
    table.add_column("부하", justify="right")
    table.add_column("비고")

    for row in summary:
        util = row["utilization_pct"]
        util_style = "red" if util >= 90 else ("yellow" if util >= 70 else "green")
        holiday_str = "[dim]휴일[/dim]" if row["is_holiday"] else ""
        table.add_row(
            row["date"],
            str(row["available_hours"]),
            str(row["scheduled_hours"]),
            str(row["free_hours"]),
            f"[{util_style}]{util}%[/{util_style}]",
            holiday_str,
        )

    console.print(table)


@app.command("reschedule")
def reschedule(
    block_id: int = typer.Argument(..., help="재일정할 블록 ID"),
    to_date: Optional[str] = typer.Option(None, "--date", help="이동할 날짜 (YYYY-MM-DD)"),
    reason: str = typer.Option("", "-r", "--reason", help="이월 사유"),
):
    """특정 블록을 다른 날로 이월합니다."""
    init_db()
    target: Optional[date] = None
    if to_date:
        try:
            target = date.fromisoformat(to_date)
        except ValueError:
            console.print(f"[red]날짜 형식 오류: {to_date}[/red]")
            raise typer.Exit(1)

    with get_session() as session:
        from core.carryover import CarryoverService
        svc = CarryoverService(session)
        try:
            new_block = svc.defer_block(block_id, to_date=target, reason=reason)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    console.print(
        f"[green]✅ 블록 이월 완료: {new_block.start_time.astimezone(tz).strftime('%m/%d %H:%M')}[/green]"
    )


@app.command("mcp-sync")
def mcp_sync(
    task_id: int = typer.Argument(..., help="동기화할 태스크 ID"),
    notion: bool = typer.Option(True, "--notion/--no-notion", help="Notion 동기화"),
    google: bool = typer.Option(True, "--google/--no-google", help="Google Calendar 동기화"),
):
    """
    태스크의 MCP 동기화 페이로드를 출력합니다.

    Claude Code가 이 출력을 읽고 MCP 도구(notion-create-pages,
    gcal_create_event)를 직접 호출한 뒤, 반환된 ID를
    PATCH /tasks/{id}/external-ids 로 저장합니다.

    사용 예:
      python main.py mcp-sync 1
      → Claude에게 "이 페이로드로 Notion과 구글 캘린더에 동기화해줘" 라고 요청
    """
    import json
    init_db()

    with get_session() as session:
        from models.task import Task
        from integrations.mcp_helper import (
            build_notion_page_payload,
            build_gcal_event_payload,
            NOTION_DB_DDL,
        )

        task = session.get(Task, task_id)
        if not task:
            console.print(f"[red]태스크 {task_id}를 찾을 수 없습니다.[/red]")
            raise typer.Exit(1)

        already_notion = bool(task.notion_page_id)
        already_google = any(b.google_event_id for b in task.schedule_blocks)

        console.print(Panel(
            f"[bold]태스크:[/bold] {task.title}\n"
            f"[bold]Notion 동기화:[/bold] {'이미 완료 (' + task.notion_page_id + ')' if already_notion else '미동기화'}'\n"
            f"[bold]Google 동기화:[/bold] {'이미 완료' if already_google else '미동기화'}",
            title="[cyan]MCP 동기화 페이로드[/cyan]",
        ))

        if notion and not already_notion:
            notion_payload = build_notion_page_payload(task)
            notion_parent: dict = {}
            if settings.notion_tasks_database_id:
                notion_parent = {"database_id": settings.notion_tasks_database_id, "type": "database_id"}
            elif settings.notion_parent_page_id:
                notion_parent = {"page_id": settings.notion_parent_page_id, "type": "page_id"}

            console.print("\n[bold yellow]── Notion: notion-create-pages 호출 파라미터 ──[/bold yellow]")
            console.print(f"[dim]parent:[/dim] {json.dumps(notion_parent, ensure_ascii=False)}")
            console.print(f"[dim]pages:[/dim]")
            console.print(json.dumps([notion_payload], ensure_ascii=False, indent=2))
            console.print(
                f"\n[dim]호출 후 반환된 page_id를 아래로 저장:[/dim]\n"
                f"  PATCH /tasks/{task_id}/external-ids\n"
                f"  Body: {{\"notion_page_id\": \"<반환된 page_id>\"}}"
            )
        elif already_notion:
            console.print(f"\n[green]Notion 이미 동기화됨: {task.notion_page_id}[/green]")

        if google and task.schedule_blocks:
            console.print("\n[bold yellow]── Google Calendar: gcal_create_event 호출 파라미터 ──[/bold yellow]")
            console.print(f"[dim]calendarId:[/dim] {settings.google_calendar_id}")
            gcal_items = []
            for block in task.schedule_blocks:
                if block.google_event_id:
                    console.print(f"  블록 {block.id}: 이미 동기화됨 ({block.google_event_id})")
                    continue
                event_payload = build_gcal_event_payload(block, task)
                gcal_items.append({"block_id": block.id, "event": event_payload})
                console.print(f"\n  [dim]블록 {block.id}:[/dim]")
                console.print(json.dumps(event_payload, ensure_ascii=False, indent=2))

            if gcal_items:
                console.print(
                    f"\n[dim]각 블록의 event_id를 아래로 저장:[/dim]\n"
                    f"  PATCH /tasks/{task_id}/external-ids\n"
                    f"  Body: {{\"google_event_ids\": ["
                    + ", ".join(f'{{\"block_id\": {i[\"block_id\"]}, \"event_id\": \"<반환값>\"}}' for i in gcal_items)
                    + "]}}"
                )
        elif not task.schedule_blocks:
            console.print("\n[yellow]배정된 일정 블록이 없습니다. 먼저 태스크를 생성하세요.[/yellow]")

        if not settings.notion_tasks_database_id and not settings.notion_parent_page_id:
            console.print(
                "\n[yellow]⚠ NOTION_TASKS_DATABASE_ID 또는 NOTION_PARENT_PAGE_ID를 .env에 설정하세요.[/yellow]\n"
                f"Notion DB가 없다면 아래 DDL로 생성 가능합니다:\n{NOTION_DB_DDL}"
            )


@app.command("serve")
def serve(
    host: str = typer.Option(settings.api_host, "--host"),
    port: int = typer.Option(settings.api_port, "--port"),
    reload: bool = typer.Option(False, "--reload"),
):
    """FastAPI 서버 및 백그라운드 스케줄러 실행."""
    import uvicorn
    from core.scheduler import SchedulerService

    init_db()
    scheduler = SchedulerService()
    scheduler.start()
    console.print(f"[green]✅ 스케줄러 시작 (알림 간격: {settings.notification_interval_hours}시간)[/green]")
    console.print(f"[cyan]🚀 API 서버: http://{host}:{port}[/cyan]")
    console.print(f"[dim]📖 API 문서: http://{host}:{port}/docs[/dim]")

    try:
        uvicorn.run(
            "api.server:app",
            host=host,
            port=port,
            reload=reload,
            log_level="debug" if settings.debug else "info",
        )
    finally:
        scheduler.stop()


# ─── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _print_task_panel(task) -> None:
    from rich.text import Text

    lines = [
        f"[bold]ID:[/bold] {task.id}",
        f"[bold]제목:[/bold] {task.title}",
        f"[bold]우선순위:[/bold] {_priority_label(task.priority)}  (점수: {task.priority_score})",
        f"[bold]마감일:[/bold] {task.deadline.astimezone(tz).strftime('%Y/%m/%d %H:%M') if task.deadline else '없음'}",
        f"[bold]예상 시간:[/bold] {task.estimated_hours}h",
    ]

    if task.subtasks:
        lines.append(f"\n[bold]세부 작업 ({len(task.subtasks)}개):[/bold]")
        for st in task.subtasks:
            lines.append(f"  {st.order}. {st.title} ({st.estimated_hours}h)")

    if task.schedule_blocks:
        lines.append(f"\n[bold]일정 블록 ({len(task.schedule_blocks)}개):[/bold]")
        for b in task.schedule_blocks[:3]:
            lines.append(
                f"  📅 {b.start_time.astimezone(tz).strftime('%m/%d %H:%M')} ~ "
                f"{b.end_time.astimezone(tz).strftime('%H:%M')} ({b.planned_hours}h)"
            )

    console.print(Panel("\n".join(lines), title="[green]✅ 태스크 생성 완료[/green]", border_style="green"))


def _priority_label(priority) -> str:
    return {
        "critical": "[red]🔴 Critical[/red]",
        "high": "[orange3]🟠 High[/orange3]",
        "medium": "[yellow]🟡 Medium[/yellow]",
        "low": "[green]🟢 Low[/green]",
    }.get(priority.value, priority.value)


def _status_label(status) -> str:
    return {
        "pending": "[dim]대기[/dim]",
        "in_progress": "[cyan]진행중[/cyan]",
        "completed": "[green]완료[/green]",
        "deferred": "[yellow]이월[/yellow]",
        "cancelled": "[red]취소[/red]",
    }.get(status.value, status.value)


def _block_status_label(status) -> str:
    return {
        "scheduled": "[cyan]예정[/cyan]",
        "active": "[bold cyan]진행중[/bold cyan]",
        "done": "[green]완료[/green]",
        "missed": "[red]미완료[/red]",
        "rescheduled": "[yellow]이월됨[/yellow]",
    }.get(status.value, status.value)


if __name__ == "__main__":
    app()
