"""MCP 동기화 헬퍼.

Claude Code가 Notion MCP / Google Calendar MCP 도구를 호출할 때 필요한
입력 포맷으로 태스크·블록 데이터를 변환한다.

대상 Notion DB: "실행목표" (1501fffda2f645ab85e5db1ef47fc80e)
  - 태그가 "구체적인 작업정리"인 항목만 schedule-agent로 관리한다.

사용 도구:
  Notion:
    notion-create-pages  → build_notion_page_payload()
    notion-update-page   → build_notion_update_payload()
    notion-search        → parse_notion_search_result()

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

# "실행목표" DB의 스키마 정보 (참고용)
NOTION_DB_DDL = """
-- 기존 "실행목표" 데이터베이스를 사용합니다.
-- DB ID: 1501fffda2f645ab85e5db1ef47fc80e
-- 태그: "구체적인 작업정리" 로 필터링하여 schedule-agent 태스크만 관리합니다.
--
-- 주요 컬럼:
--   작업 이름  TEXT (TITLE)
--   상태       TEXT ('시작 전' | '진행 중' | '완료' | '보관됨')
--   태그       TEXT[] ('구체적인 작업정리' 포함)
--   실행기간   DATE RANGE (start/end)
""".strip()

# schedule-agent 상태 → 실행목표 상태 매핑
_STATUS_LABEL = {
    "pending": "시작 전",
    "in_progress": "진행 중",
    "completed": "완료",
    "deferred": "시작 전",
    "cancelled": "보관됨",
}

# 우선순위 이모지 (페이지 본문에 표시)
_PRIORITY_LABEL = {
    "critical": "🔴 Critical",
    "high": "🟠 High",
    "medium": "🟡 Medium",
    "low": "🟢 Low",
}

# schedule-agent 태그 식별자
_SCHEDULE_AGENT_TAG = "구체적인 작업정리"


def build_notion_page_payload(task) -> dict:
    """
    Task 객체 → notion-create-pages 의 pages[] 항목 1개.

    "실행목표" DB 스키마에 맞게 변환한다.
    태그에 "구체적인 작업정리"를 자동 포함한다.

    Claude 사용 예:
        payload = build_notion_page_payload(task)
        # notion-create-pages 호출:
        #   parent = {"database_id": "1501fffda2f645ab85e5db1ef47fc80e"}
        #   pages  = [payload]
    """
    properties: dict = {
        "작업 이름": task.title,
        "상태": _STATUS_LABEL.get(task.status.value, "시작 전"),
        "태그": json_dumps([_SCHEDULE_AGENT_TAG]),
    }

    # 실행기간: deadline을 기간 시작일로 설정
    if task.deadline:
        deadline_str = task.deadline.astimezone(_tz).strftime("%Y-%m-%d")
        properties["date:실행기간:start"] = deadline_str

    # 페이지 본문: 우선순위·예상시간·설명·서브태스크를 마크다운으로
    content_parts = []

    priority_label = _PRIORITY_LABEL.get(task.priority.value, "🟡 Medium")
    hours = task.estimated_hours or 0
    content_parts.append(f"**우선순위:** {priority_label}  |  **예상 소요시간:** {hours}h")

    if task.description:
        clean = task.description.replace("[AI 분석]", "").strip()
        if clean:
            content_parts.append(f"\n**설명:**\n{clean[:500]}")

    if task.subtasks:
        content_parts.append("\n## 세부 작업")
        for st in sorted(task.subtasks, key=lambda s: s.order):
            done = "x" if st.status.value == "completed" else " "
            content_parts.append(f"- [{done}] {st.title} ({st.estimated_hours}h)")

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
        #   page_id    = task.notion_page_id
        #   command    = "update_properties"
        #   properties = props
    """
    props: dict = {
        "상태": _STATUS_LABEL.get(task.status.value, "시작 전"),
    }
    if task.status.value == "completed":
        # 완료 시 기간 내 달성 여부 표시 (마감일 기준 판단 생략, 기본값 사용)
        props["기간 내 달성"] = "기간 내 달성"
    return props


def parse_notion_search_result(results: list[dict]) -> list[dict]:
    """
    notion-search 결과 → /tasks (POST) 로 임포트할 수 있는 dict 목록.

    "구체적인 작업정리" 태그가 있는 항목만 반환한다.

    각 항목:
      title, description, deadline, priority, notion_page_id
    """
    tasks = []
    for item in results:
        if item.get("type") != "page":
            continue
        props = item.get("properties", {})

        # 태그 필터: "구체적인 작업정리" 가 없으면 건너뜀
        tags = _extract_multi_select(props.get("태그", {}))
        if _SCHEDULE_AGENT_TAG not in tags:
            continue

        title = _extract_title_by_key(props, "작업 이름")
        if not title:
            title = item.get("title") or item.get("url", "")

        # 실행기간 → deadline
        deadline = None
        exec_period = props.get("실행기간", {}).get("date", {})
        if exec_period.get("start"):
            deadline = exec_period["start"]

        tasks.append({
            "title": title,
            "description": "",
            "deadline": deadline,
            "priority": "medium",  # 실행목표 DB엔 priority 없음
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

import json as _json


def json_dumps(value) -> str:
    """notion-create-pages의 multi_select 컬럼에 JSON 배열 전달."""
    return _json.dumps(value, ensure_ascii=False)


def _extract_title_by_key(props: dict, key: str) -> str:
    rich = props.get(key, {}).get("title", [])
    if rich:
        return "".join(r.get("plain_text", "") for r in rich)
    return ""


def _extract_title(props: dict) -> str:
    for key in ("작업 이름", "Name", "Title", "title", "name"):
        rich = props.get(key, {}).get("title", [])
        if rich:
            return "".join(r.get("plain_text", "") for r in rich)
    return ""


def _extract_rich_text(prop: dict) -> str:
    rich = prop.get("rich_text", [])
    return "".join(r.get("plain_text", "") for r in rich)


def _extract_multi_select(prop: dict) -> list[str]:
    options = prop.get("multi_select", [])
    return [o.get("name", "") for o in options]


def _parse_rfc3339(s: str) -> datetime:
    """RFC3339 문자열을 timezone-aware datetime으로 파싱."""
    from dateutil import parser as dtparser
    dt = dtparser.parse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz)
    return dt
