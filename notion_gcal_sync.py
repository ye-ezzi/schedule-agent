#!/usr/bin/env python3
"""
Notion 실행목표 DB → Google Calendar 자동 동기화

실행 조건:
  - 태그: 구체적인 작업정리
  - 상태: 시작 전 또는 진행 중
  - GCal 동기화됨: 체크 안 된 항목만 (중복 방지)
  - 실행기간(날짜) + 예상시간이 입력된 항목만

완료 처리:
  - Notion 상태를 "완료"로 변경 시 다음 실행 때 GCal 이벤트 자동 삭제

최초 1회: python3 google_auth_setup.py 실행 필요
cron 예시:
  0 8  * * * cd /Users/iyeji/schedule-agent && /opt/homebrew/bin/python3 notion_gcal_sync.py >> logs/sync.log 2>&1
  0 15 * * * cd /Users/iyeji/schedule-agent && /opt/homebrew/bin/python3 notion_gcal_sync.py >> logs/sync.log 2>&1
  0 22 * * * cd /Users/iyeji/schedule-agent && /opt/homebrew/bin/python3 notion_gcal_sync.py >> logs/sync.log 2>&1
"""
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from notion_client import Client

# ─── 설정 ──────────────────────────────────────────────────────────────────

NOTION_DB_ID = "1501fffda2f645ab85e5db1ef47fc80e"
TAG = "구체적인 작업정리"
CALENDAR_ID = "primary"
TIMEZONE = "Asia/Seoul"
WORK_START_HOUR = 9
WORK_END_HOUR = 22

# Notion 동기화 추적 컬럼
GCAL_SYNCED_PROP = "GCal 동기화됨"      # CHECKBOX
GCAL_EVENT_IDS_PROP = "GCal 이벤트 ID"  # RICH_TEXT (콤마 구분)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "google_token.json")
CREDS_FILE = os.path.join(BASE_DIR, "google_credentials.json")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# P0~P4 → GCal colorId + 이모지 + 정렬 순서
PRIORITY_CONFIG = {
    "P0": {"color": "11", "emoji": "🔴", "order": 0},  # Tomato
    "P1": {"color": "6",  "emoji": "🟠", "order": 1},  # Tangerine
    "P2": {"color": "5",  "emoji": "🟡", "order": 2},  # Banana
    "P3": {"color": "2",  "emoji": "🟢", "order": 3},  # Sage
    "P4": {"color": "8",  "emoji": "⚪", "order": 4},  # Graphite
}

KST = timezone(timedelta(hours=9))


# ─── 환경변수 로드 ─────────────────────────────────────────────────────────

def load_env():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ─── Notion ────────────────────────────────────────────────────────────────

def get_notion_client() -> Client:
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        print("❌ NOTION_API_KEY가 .env에 없습니다.")
        sys.exit(1)
    return Client(auth=api_key)


def ensure_notion_properties(notion: Client):
    """Notion DB에 동기화 추적 컬럼이 없으면 자동 추가."""
    db = notion.databases.retrieve(database_id=NOTION_DB_ID)
    props = db["properties"]
    update_props = {}
    if GCAL_SYNCED_PROP not in props:
        update_props[GCAL_SYNCED_PROP] = {"checkbox": {}}
    if GCAL_EVENT_IDS_PROP not in props:
        update_props[GCAL_EVENT_IDS_PROP] = {"rich_text": {}}
    if update_props:
        notion.databases.update(database_id=NOTION_DB_ID, properties=update_props)
        print(f"✅ Notion DB 컬럼 추가: {list(update_props.keys())}")


def get_tasks(notion: Client) -> list[dict]:
    """GCal 동기화됨=False인 태스크만 반환 (중복 방지)."""
    resp = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {"property": "태그", "multi_select": {"contains": TAG}},
                {
                    "or": [
                        {"property": "상태", "status": {"equals": "시작 전"}},
                        {"property": "상태", "status": {"equals": "진행 중"}},
                    ]
                },
                {"property": GCAL_SYNCED_PROP, "checkbox": {"equals": False}},
            ]
        },
    )

    today = date.today().isoformat()
    tasks = []
    for page in resp.get("results", []):
        p = page["properties"]
        page_id = page["id"]

        title_arr = p.get("작업 이름", {}).get("title", [])
        if not title_arr:
            continue
        name = title_arr[0]["plain_text"].strip()

        date_info = (p.get("실행기간") or {}).get("date") or {}
        start_date = (date_info.get("start") or "")[:10]
        end_date = (date_info.get("end") or "")[:10]

        if not start_date:
            continue

        total_hours = (p.get("예상시간") or {}).get("number") or 1.0

        priority = (p.get("선택") or {}).get("select") or {}
        priority_name = (priority.get("name") or "").strip()
        p_config = PRIORITY_CONFIG.get(priority_name, {"color": "6", "emoji": "🟡", "order": 99})

        # 날짜 범위 계산
        start_dt = date.fromisoformat(start_date)
        end_dt = date.fromisoformat(end_date) if end_date else start_dt
        if end_dt < start_dt:
            end_dt = start_dt

        # 범위 내 모든 날짜 생성 (오늘 이전 날짜 제외)
        all_days = []
        current = start_dt
        while current <= end_dt:
            if current.isoformat() >= today:
                all_days.append(current.isoformat())
            current += timedelta(days=1)

        if not all_days:
            continue

        # 하루치 시간 = 총 예상시간 / 전체 날짜 수 (소수점 2자리 반올림)
        total_days = (end_dt - start_dt).days + 1
        hours_per_day = round(total_hours / total_days, 2)

        for day in all_days:
            tasks.append({
                "notion_page_id": page_id,
                "name": name,
                "date": day,
                "hours": hours_per_day,
                "priority": priority_name,
                "color": p_config["color"],
                "emoji": p_config["emoji"],
                "order": p_config["order"],
            })

    tasks.sort(key=lambda t: (t["date"], t["order"]))
    return tasks


def update_notion_sync_status(notion: Client, page_id: str, event_ids: list[str]):
    """GCal 이벤트 생성 완료 후 Notion 페이지에 체크박스 + 이벤트 ID 저장."""
    notion.pages.update(
        page_id=page_id,
        properties={
            GCAL_SYNCED_PROP: {"checkbox": True},
            GCAL_EVENT_IDS_PROP: {
                "rich_text": [{"text": {"content": ",".join(event_ids)}}]
            },
        },
    )


def cleanup_completed_tasks(service, notion: Client, dry_run: bool = False):
    """완료된 태스크의 GCal 이벤트 삭제 후 Notion 필드 초기화."""
    resp = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {"property": "태그", "multi_select": {"contains": TAG}},
                {"property": "상태", "status": {"equals": "완료"}},
                {"property": GCAL_EVENT_IDS_PROP, "rich_text": {"is_not_empty": True}},
            ]
        },
    )

    pages = resp.get("results", [])
    if not pages:
        return

    print(f"🗑️  완료 태스크 정리: {len(pages)}개")
    for page in pages:
        p = page["properties"]
        page_id = page["id"]
        name_arr = p.get("작업 이름", {}).get("title", [])
        name = name_arr[0]["plain_text"].strip() if name_arr else "(이름 없음)"
        event_ids_str = "".join(
            r["plain_text"] for r in p.get(GCAL_EVENT_IDS_PROP, {}).get("rich_text", [])
        )
        event_ids = [e for e in event_ids_str.split(",") if e]

        print(f"  → 🗑️  {name} (이벤트 {len(event_ids)}개) ... ", end="", flush=True)
        if dry_run:
            print("[DRY RUN]")
            continue

        failed = False
        for eid in event_ids:
            try:
                service.events().delete(calendarId=CALENDAR_ID, eventId=eid).execute()
            except Exception as e:
                print(f"\n     ⚠️  이벤트 삭제 실패 ({eid}): {e}")
                failed = True

        if not failed:
            notion.pages.update(
                page_id=page_id,
                properties={
                    GCAL_SYNCED_PROP: {"checkbox": False},
                    GCAL_EVENT_IDS_PROP: {"rich_text": []},
                },
            )
            print("✅")
        else:
            print("⚠️  일부 실패 (Notion 필드 유지)")


# ─── Google Calendar ───────────────────────────────────────────────────────

def get_gcal_service():
    if not os.path.exists(TOKEN_FILE):
        print("❌ google_token.json 없음. 먼저 실행하세요: python3 google_auth_setup.py")
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def get_busy_slots(service, day: str) -> list[tuple]:
    """해당 날짜의 기존 이벤트 시간 목록 반환."""
    start = f"{day}T00:00:00+09:00"
    end = f"{day}T23:59:59+09:00"
    events = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])

    busy = []
    for e in events:
        s = e["start"].get("dateTime")
        en = e["end"].get("dateTime")
        if s and en:
            busy.append((datetime.fromisoformat(s), datetime.fromisoformat(en)))
    return busy


def find_free_slot(busy: list[tuple], day: str, hours: float) -> tuple[datetime, datetime]:
    """업무 시간 내 첫 번째 빈 슬롯 반환. 없으면 WORK_START_HOUR 사용."""
    duration = timedelta(hours=hours)
    work_start = datetime.fromisoformat(f"{day}T{WORK_START_HOUR:02d}:00:00+09:00")
    work_end = datetime.fromisoformat(f"{day}T{WORK_END_HOUR:02d}:00:00+09:00")

    candidate = work_start
    for busy_start, busy_end in sorted(busy):
        if candidate + duration <= busy_start:
            return candidate, candidate + duration
        if busy_end > candidate:
            candidate = busy_end

    if candidate + duration <= work_end:
        return candidate, candidate + duration

    return work_start, work_start + duration


def create_event(service, task: dict) -> str:
    """Google Calendar 이벤트 생성 후 event_id 반환."""
    busy = get_busy_slots(service, task["date"])
    start_dt, end_dt = find_free_slot(busy, task["date"], task["hours"])

    title = f"{task['emoji']} {task['name']} ({task['hours']}h)"
    event = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
        "colorId": task["color"],
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
                {"method": "popup", "minutes": 10},
            ],
        },
    }
    result = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return result["id"]


# ─── 메인 ──────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="실제 생성 없이 동기화 대상만 출력")
    args = parser.parse_args()

    load_env()
    notion = get_notion_client()
    ensure_notion_properties(notion)

    today = date.today().isoformat()
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    service = get_gcal_service()

    # 1. 완료 태스크 GCal 이벤트 정리
    cleanup_completed_tasks(service, notion, dry_run=args.dry_run)

    # 2. 동기화 안 된 태스크 조회
    tasks = get_tasks(notion)
    print(f"[{today}] 동기화 대상: {len(tasks)}개")

    if not tasks:
        return

    if args.dry_run:
        print("🔍 [DRY RUN] 실제 이벤트는 생성되지 않습니다.\n")
        for task in tasks:
            priority_label = f"[{task['priority']}] " if task['priority'] else ""
            print(f"  → {task['emoji']} {priority_label}{task['name']} ({task['date']}, {task['hours']}h)")
        return

    # 3. 이벤트 생성 (page_id별로 event_id 수집)
    page_event_ids: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        priority_label = f"[{task['priority']}] " if task['priority'] else ""
        print(f"  → {task['emoji']} {priority_label}{task['name']} ({task['date']}, {task['hours']}h) ... ", end="", flush=True)
        try:
            event_id = create_event(service, task)
            page_event_ids[task["notion_page_id"]].append(event_id)
            print(f"✅ (event_id: {event_id})")
        except Exception as e:
            print(f"❌ {e}")

    # 4. Notion 동기화 상태 업데이트
    for page_id, event_ids in page_event_ids.items():
        try:
            update_notion_sync_status(notion, page_id, event_ids)
        except Exception as e:
            print(f"⚠️  Notion 업데이트 실패 ({page_id}): {e}")


if __name__ == "__main__":
    main()
