#!/usr/bin/env python3
"""
Notion 실행목표 DB → Google Calendar 자동 동기화

실행 조건:
  - 태그: 구체적인 작업정리
  - 상태: 시작 전 또는 진행 중
  - 실행기간(날짜) + 예상시간이 입력된 항목만

최초 1회: python3 google_auth_setup.py 실행 필요
cron 예시:
  0 8 * * * cd /Users/iyeji/schedule-agent && python3 notion_gcal_sync.py >> logs/sync.log 2>&1
"""
import os
import sys
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

    # 마지막 이벤트 이후에 공간이 있으면 사용
    if candidate + duration <= work_end:
        return candidate, candidate + duration

    # 없으면 기본값
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


# ─── Notion ────────────────────────────────────────────────────────────────

def get_tasks() -> list[dict]:
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        print("❌ NOTION_API_KEY가 .env에 없습니다.")
        sys.exit(1)

    notion = Client(auth=api_key)
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
            ]
        },
    )

    today = date.today().isoformat()
    tasks = []
    for page in resp.get("results", []):
        p = page["properties"]

        title_arr = p.get("작업 이름", {}).get("title", [])
        if not title_arr:
            continue
        name = title_arr[0]["plain_text"].strip()

        date_info = (p.get("실행기간") or {}).get("date") or {}
        task_date = (date_info.get("start") or "")[:10]
        if not task_date or task_date < today:
            continue

        hours = (p.get("예상시간") or {}).get("number") or 1.0

        priority = (p.get("선택") or {}).get("select") or {}
        priority_name = (priority.get("name") or "").strip()
        p_config = PRIORITY_CONFIG.get(priority_name, {"color": "6", "emoji": "🟡", "order": 99})

        tasks.append({
            "name": name,
            "date": task_date,
            "hours": hours,
            "priority": priority_name,
            "color": p_config["color"],
            "emoji": p_config["emoji"],
            "order": p_config["order"],
        })

    tasks.sort(key=lambda t: (t["date"], t["order"]))
    return tasks


# ─── 메인 ──────────────────────────────────────────────────────────────────

def main():
    load_env()
    tasks = get_tasks()
    today = date.today().isoformat()
    print(f"[{today}] 동기화 대상: {len(tasks)}개")

    if not tasks:
        return

    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    service = get_gcal_service()

    for task in tasks:
        priority_label = f"[{task['priority']}] " if task['priority'] else ""
        print(f"  → {task['emoji']} {priority_label}{task['name']} ({task['date']}, {task['hours']}h) ... ", end="", flush=True)
        try:
            event_id = create_event(service, task)
            print(f"✅ (event_id: {event_id})")
        except Exception as e:
            print(f"❌ {e}")


if __name__ == "__main__":
    main()
