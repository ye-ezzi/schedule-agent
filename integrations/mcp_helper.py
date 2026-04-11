"""MCP 동기화 헬퍼.

Claude Code가 Notion MCP / Google Calendar MCP 도구를 호출할 때 필요한
입력 포맷으로 태스크·블록 데이터를 변환한다.

사용 도구:
  Notion:
    notion-create-pages  → build_notion_page_payload()
    notion-update-page   → build_notion_update_payload()
    notion-search        → parse_notion_search_result()
    notion-create-database → NOTION_DB_DDL

  Google Calendar:
    gcal_create_event      → build_gcal_event_payload()
    gcal_update_event      → build_gcal_update_payload()
    gcal_find_my_free_time → parse_gcal_free_slots()
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pytz

from config import settings

_tz = pytz.timezone(settings.timezone)

# ─── Notion ───────────────────────────────────────────────────────────────────

# Notion DB를 처음 만들 때 사용하는 SQL DDL
NOTION_DB_DDL = """
CREATE TABLE tasks (
  Name TEXT NOT NULL,
  Status TEXT DEFAULT 'Not Started',
  Priority TEXT DEFAULT '🟡 Medium',
  Deadline DATE,
  "Estimated Hours" NUMBER,
  "Priority Score" NUMBER,
  Description TEXT,
  "Completed At" DATE
);
""".strip()

_PRIORITY_LABEL = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
}

_STATUS_LABEL = {
    "pending": "Not Started",
    "in_progress": "In Progress",
    "completed": "Done",
    "deferred": "Deferred",
    "cancelled": "Cancelled",
}


def build_notion_page_payload(task) -> dict:
    """
    Task 객체 → notion-create-pages 의 pages[] 항목 1개.

    Claude 사용 예:
        payload = build_notion_page_payload(task)
        # notion-create-pages 호출:
        #   parent = {"database_id": settings.notion_tasks_database_id}
        #   pages  = [payload]
    """
    properties: dict = {
        "Name": task.title,
        "Status": _STATUS_LABEL.get(task.status.value, "Not Started"),
        "Priority": _PRIORITY_LABEL.get(task.priority.value, "🟡 Medium"),
        "Estimated Hours": task.estimated_hours or 0,
        "Priority Score": task.priority_score or 0,
    }
    if task.deadline:
        properties["Deadline"] = task.deadline.astimezone(_tz).strftime("%Y-%m-%d")
    if task.description:
        # AI 분석 텍스트는 Description에 포함하되 500자 제한
        clean_desc = task.description.replace("[AI 분석]", "").strip()
        properties["Description"] = clean_desc[:500]

    # 페이지 본문 마크다운 생성
    content_parts = []
    if task.subtasks:
        content_parts.append("## 세부 작업")
        for st in sorted(task.subtasks, key=lambda s: s.order):
            done = "x" if st.status.value == "completed" else " "
            content_parts.append(f"- [{done}] {st.title} ({st.estimated_hours}h)")

    # AI 분석 고려 사항 추출
    if task.description and "[AI 분석]" in task.description:
        ai_notes = task.description.split("[AI 분석]", 1)[1].strip()
        if ai_notes:
            content_parts.append("\n## AI 분석")
            content_parts.append(ai_notes[:1000])

    if task.carry_over_count > 0:
        content_parts.append(f"\n> 이월 횟수: {task.carry_over_count}회")

    return {
        "properties": properties,
        "content": "\n".join(content_parts) if content_parts else "",
    }


def build_notion_update_payload(task) -> dict:
    """
    Task 상태 변경 → notion-update-page 의 properties 파라미터.

    Claude 사용 예:
        props = build_notion_update_payload(task)
        # notion-update-page 호출:
        #   page_id   = task.notion_page_id
        #   command   = "update_properties"
        #   properties = props
    """
    props: dict = {
        "Status": _STATUS_LABEL.get(task.status.value, "Not Started"),
        "Priority": _PRIORITY_LABEL.get(task.priority.value, "🟡 Medium"),
        "Priority Score": task.priority_score or 0,
    }
    if task.status.value == "completed":
        props["Completed At"] = datetime.now(_tz).strftime("%Y-%m-%d")
    if task.estimated_hours:
        props["Estimated Hours"] = task.estimated_hours
    return props


def parse_notion_search_result(results: list[dict]) -> list[dict]:
    """
    notion-search 결과 → /tasks (POST) 로 임포트할 수 있는 dict 목록.

    각 항목:
      title, description, deadline, priority, notion_page_id
    """
    tasks = []
    for item in results:
        if item.get("type") != "page":
            continue
        props = item.get("properties", {})

        title = _extract_title(props)
        if not title:
            # 일반 페이지(DB 항목 아닌 것)는 page title 사용
            title = item.get("title") or item.get("url", "")

        deadline = None
        if props.get("Deadline", {}).get("date", {}).get("start"):
            try:
                deadline = props["Deadline"]["date"]["start"]
            except (KeyError, TypeError):
                pass

        priority_label = (
            props.get("Priority", {}).get("select", {}).get("name") or "🟡 Medium"
        )
        priority_map = {v: k for k, v in _PRIORITY_LABEL.items()}
        priority = priority_map.get(priority_label, "medium")

        tasks.append({
            "title": title,
            "description": _extract_rich_text(props.get("Description", {})),
            "deadline": deadline,
            "priority": priority,
            "notion_page_id": item.get("id", ""),
        })
    return tasks


# ─── Google Calendar ──────────────────────────────────────────────────────────

_PRIORITY_COLOR = {
    "critical": "11",  # Tomato
    "high": "6",       # Tangerine
    "medium": "5",     # Banana
    "low": "2",        # Sage
}

_PRIORITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}

_BLOCK_STATUS_PREFIX = {
    "scheduled": "",
    "active": "▶️ ",
    "done": "✅ ",
    "missed": "⚠️ ",
    "rescheduled": "🔄 ",
}


def build_gcal_event_payload(block, task) -> dict:
    """
    ScheduleBlock + Task → gcal_create_event 의 event 파라미터.

    Claude 사용 예:
        event = build_gcal_event_payload(block, task)
        # gcal_create_event 호출:
        #   calendarId = settings.google_calendar_id (또는 "primary")
        #   event      = event
    """
    priority_emoji = _PRIORITY_EMOJI.get(task.priority.value, "📌")
    status_prefix = _BLOCK_STATUS_PREFIX.get(block.status.value, "")
    summary = f"{status_prefix}{priority_emoji} {task.title} ({block.planned_hours}h)"

    desc_parts = []
    if task.description:
        clean = task.description.replace("[AI 분석]", "").strip()
        if clean:
            desc_parts.append(clean[:300])
    if task.subtasks:
        remaining = [
            f"• {s.title}" for s in task.subtasks if s.status.value != "completed"
        ]
        if remaining:
            desc_parts.append("남은 세부 작업:\n" + "\n".join(remaining))
    if block.reschedule_count > 0:
        desc_parts.append(f"이월 횟수: {block.reschedule_count}회")
    if task.carry_over_count > 0:
        desc_parts.append(f"태스크 이월 총 횟수: {task.carry_over_count}회")

    start_iso = block.start_time.astimezone(_tz).isoformat()
    end_iso = block.end_time.astimezone(_tz).isoformat()

    return {
        "summary": summary,
        "description": "\n\n".join(desc_parts),
        "start": {"dateTime": start_iso, "timeZone": settings.timezone},
        "end": {"dateTime": end_iso, "timeZone": settings.timezone},
        "colorId": _PRIORITY_COLOR.get(task.priority.value, "1"),
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 10},
            ],
        },
    }


def build_gcal_update_payload(block, task) -> dict:
    """
    재일정된 블록 → gcal_update_event 의 event 파라미터 (변경 필드만).

    Claude 사용 예:
        update = build_gcal_update_payload(block, task)
        # gcal_update_event 호출:
        #   calendarId = settings.google_calendar_id
        #   eventId    = block.google_event_id
        #   event      = update
    """
    status_prefix = _BLOCK_STATUS_PREFIX.get(block.status.value, "")
    priority_emoji = _PRIORITY_EMOJI.get(task.priority.value, "📌")
    return {
        "summary": f"{status_prefix}{priority_emoji} {task.title} ({block.planned_hours}h)",
        "start": {
            "dateTime": block.start_time.astimezone(_tz).isoformat(),
            "timeZone": settings.timezone,
        },
        "end": {
            "dateTime": block.end_time.astimezone(_tz).isoformat(),
            "timeZone": settings.timezone,
        },
        "colorId": _PRIORITY_COLOR.get(task.priority.value, "1"),
    }


def parse_gcal_free_slots(
    free_time_result: dict,
    min_hours: float = 1.0,
) -> list[dict]:
    """
    gcal_find_my_free_time 결과 → POST /schedule/capacity-from-gcal 의 free_slots.

    gcal_find_my_free_time 반환 구조 (예상):
      {"free_slots": [{"start": "...", "end": "..."}]}

    반환:
      [{"date": "YYYY-MM-DD", "free_hours": float}, ...]
    """
    # 날짜별로 빈 시간을 합산
    daily: dict[str, float] = {}

    slots_raw = (
        free_time_result.get("free_slots")
        or free_time_result.get("freeSlots")
        or free_time_result.get("slots")
        or []
    )

    for slot in slots_raw:
        start_str = slot.get("start") or slot.get("startTime", "")
        end_str = slot.get("end") or slot.get("endTime", "")
        if not start_str or not end_str:
            continue
        try:
            start_dt = _parse_rfc3339(start_str)
            end_dt = _parse_rfc3339(end_str)
            hours = (end_dt - start_dt).total_seconds() / 3600
            if hours < min_hours:
                continue
            day_key = start_dt.astimezone(_tz).date().isoformat()
            daily[day_key] = round(daily.get(day_key, 0) + hours, 2)
        except (ValueError, TypeError):
            continue

    return [{"date": d, "free_hours": h} for d, h in sorted(daily.items())]


# ─── 내부 유틸 ────────────────────────────────────────────────────────────────

def _extract_title(props: dict) -> str:
    for key in ("Name", "Title", "title", "name"):
        rich = props.get(key, {}).get("title", [])
        if rich:
            return "".join(r.get("plain_text", "") for r in rich)
    return ""


def _extract_rich_text(prop: dict) -> str:
    rich = prop.get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _parse_rfc3339(s: str) -> datetime:
    """RFC3339 문자열을 timezone-aware datetime으로 파싱."""
    from dateutil import parser as dtparser
    dt = dtparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz)
    return dt
