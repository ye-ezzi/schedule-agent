#!/usr/bin/env python3
"""
Notion 실행목표 DB → Google Calendar 자동 동기화

실행 조건:
  - 태그: 구체적인 작업정리
  - 상태: 시작 전 또는 진행 중
  - 실행기간(날짜) + 예상시간이 입력된 항목만

cron 예시:
  0 8 * * * cd /Users/iyeji/schedule-agent && python3 notion_gcal_sync.py >> logs/sync.log 2>&1
"""
import os
import shutil
import subprocess
import sys
from datetime import date
from notion_client import Client

# claude CLI 경로 자동 탐색 (nvm/homebrew 등 다양한 설치 경로 대응)
def _find_claude() -> str:
    # 1. PATH에서 먼저 탐색
    found = shutil.which("claude")
    if found:
        return found
    # 2. 일반적인 설치 경로 순서대로 탐색
    candidates = [
        os.path.expanduser("~/.nvm/versions/node/v20/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        os.path.expanduser("~/.npm-global/bin/claude"),
    ]
    # nvm 동적 경로 (버전 무관)
    nvm_dir = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_dir):
        for ver in sorted(os.listdir(nvm_dir), reverse=True):
            candidates.append(os.path.join(nvm_dir, ver, "bin", "claude"))

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return "claude"  # fallback

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", _find_claude())

NOTION_DB_ID = "1501fffda2f645ab85e5db1ef47fc80e"
TAG = "구체적인 작업정리"
WORK_START = "09:00:00"
WORK_END = "22:00:00"

# P0~P4 → GCal colorId + 이모지 + 정렬 순서
PRIORITY_CONFIG = {
    "P0": {"color": "11", "emoji": "🔴", "order": 0},  # Tomato
    "P1": {"color": "6",  "emoji": "🟠", "order": 1},  # Tangerine
    "P2": {"color": "5",  "emoji": "🟡", "order": 2},  # Banana
    "P3": {"color": "2",  "emoji": "🟢", "order": 3},  # Sage
    "P4": {"color": "8",  "emoji": "⚪", "order": 4},  # Graphite
}


def get_tasks() -> list[dict]:
    """Notion에서 동기화 대상 태스크 조회."""
    api_key = os.environ.get("NOTION_API_KEY", "")
    if not api_key:
        print("❌ NOTION_API_KEY가 .env에 없습니다.")
        sys.exit(1)

    notion = Client(auth=api_key)
    resp = notion.databases.query(
        database_id=NOTION_DB_ID,
        filter={
            "and": [
                {
                    "property": "태그",
                    "multi_select": {"contains": TAG},
                },
                {
                    "or": [
                        {"property": "상태", "status": {"equals": "시작 전"}},
                        {"property": "상태", "status": {"equals": "진행 중"}},
                    ]
                },
            ]
        },
    )

    tasks = []
    for page in resp.get("results", []):
        p = page["properties"]

        # 작업 이름
        title_arr = p.get("작업 이름", {}).get("title", [])
        if not title_arr:
            continue
        name = title_arr[0]["plain_text"].strip()

        # 실행기간 (날짜 필수)
        date_info = (p.get("실행기간") or {}).get("date") or {}
        task_date = (date_info.get("start") or "")[:10]
        if not task_date:
            continue

        # 과거 날짜 건너뜀
        if task_date < date.today().isoformat():
            continue

        # 예상시간 (없으면 1h)
        hours = (p.get("예상시간") or {}).get("number") or 1.0

        # 중요도 (선택 컬럼: P0~P4)
        priority = (p.get("선택") or {}).get("select", {})
        priority_name = (priority.get("name") or "").strip() if priority else ""
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

    # P0 우선 순으로 정렬
    tasks.sort(key=lambda t: (t["date"], t["order"]))
    return tasks


def sync_to_gcal(task: dict) -> bool:
    """Claude CLI를 통해 GCal에 이벤트 생성."""
    title = f"{task['emoji']} {task['name']} ({task['hours']}h)"
    priority_note = f"중요도: {task['priority']}" if task['priority'] else ""

    prompt = (
        f"구글 캘린더에 다음 태스크를 추가해줘:\n"
        f"- 제목: {title}\n"
        f"- 날짜: {task['date']}\n"
        f"- 예상시간: {task['hours']}시간\n"
        + (f"- {priority_note}\n" if priority_note else "") +
        f"\n방법:\n"
        f"1. gcal_find_my_free_time으로 {task['date']} {WORK_START}~{WORK_END} 빈 시간 조회 (minDuration={int(task['hours']*60)}분)\n"
        f"2. 첫 번째 빈 슬롯 시작 시간에 {task['hours']}시간짜리 이벤트 생성\n"
        f"3. colorId={task['color']}, timeZone=Asia/Seoul\n"
        f"빈 시간이 없으면 {task['date']} 09:00에 생성해줘."
    )

    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt, "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(f"\n    [STDERR] {result.stderr[:300]}")
        print(f"    [STDOUT] {result.stdout[:300]}")
    return result.returncode == 0


def main():
    # .env 로드
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    tasks = get_tasks()
    today = date.today().isoformat()
    print(f"[{today}] 동기화 대상: {len(tasks)}개")

    if not tasks:
        return

    # logs 폴더 생성
    os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)

    for task in tasks:
        priority_label = f"[{task['priority']}] " if task['priority'] else ""
        print(f"  → {task['emoji']} {priority_label}{task['name']} ({task['date']}, {task['hours']}h) ... ", end="", flush=True)
        try:
            ok = sync_to_gcal(task)
            print("✅" if ok else "❌ 실패")
        except subprocess.TimeoutExpired:
            print("❌ 타임아웃")
        except FileNotFoundError:
            print(f"❌ claude CLI를 찾을 수 없습니다. (탐색 경로: {CLAUDE_BIN})")
            print("    'which claude' 로 경로 확인 후 CLAUDE_BIN 변수에 직접 입력하세요.")
            break


if __name__ == "__main__":
    main()
