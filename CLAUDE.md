# Schedule Agent — Claude Code 운영 가이드

이 프로젝트는 **Claude Code**가 MCP 도구를 통해 Notion과 Google Calendar를 직접 조작하는 방식으로 동기화합니다.
API 키·OAuth 파일 없이 이미 인증된 MCP 세션을 재사용합니다.

---

## 아키텍처 한 줄 요약

```
Python 서버(SQLite) ←REST→ Claude Code ←MCP→ Notion / Google Calendar
```

- **Python 서버**: 태스크 저장·AI 분해·우선순위·이월·알림
- **Claude Code**: MCP 도구로 외부 동기화 실행, 반환된 ID를 API로 저장

---

## 1. 초기 설정 (최초 1회)

### 1-1. Notion 데이터베이스 생성

Notion에 태스크 DB가 없으면 `notion-create-database` MCP 도구로 생성합니다.

```
도구: notion-create-database
파라미터:
  title: "Schedule Agent Tasks"
  parent: {"page_id": "<원하는 Notion 페이지 ID>", "type": "page_id"}
  schema: |
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
```

생성 후 반환된 `database_id`를 `.env`의 `NOTION_TASKS_DATABASE_ID`에 저장합니다.

### 1-2. Google Calendar 확인

```
도구: gcal_list_calendars
```

사용할 캘린더 ID를 확인하고 `.env`의 `GOOGLE_CALENDAR_ID`에 저장합니다 (기본값: `primary`).

---

## 2. 태스크 동기화 워크플로

### 방법 A: CLI 페이로드 출력 → Claude가 MCP 호출

```bash
# 1. 태스크 생성
python main.py add "앱 랜딩페이지 디자인" --deadline 2026-04-20 --priority high

# 2. 페이로드 출력 (task_id 확인 후)
python main.py mcp-sync 1
```

출력된 페이로드를 보고 Claude가 MCP 도구를 호출합니다.

### 방법 B: API 페이로드 조회 → Claude가 순서대로 실행

사용자가 "태스크 1번 노션이랑 구글 캘린더에 동기화해줘"라고 요청하면:

**Step 1. 페이로드 조회**
```
GET /tasks/1/mcp-payload
```

**Step 2-A. Notion 페이지 생성** (`already_synced.notion == false` 일 때)
```
도구: notion-create-pages
파라미터:
  parent: <응답의 notion_parent>
  pages:  [<응답의 notion_payload>]
```
반환값에서 `id` (page_id) 추출.

**Step 2-B. ID 저장**
```
PATCH /tasks/1/external-ids
Body: {"notion_page_id": "<반환된 id>"}
```

**Step 3-A. Google Calendar 이벤트 생성** (`already_synced.google == false` 이고 `gcal_payloads` 존재 시)

gcal_payloads 배열을 순회하며 각 블록에 대해:
```
도구: gcal_create_event
파라미터:
  calendarId: <응답의 gcal_calendar_id>
  event:      <gcal_payloads[i].event>
```
반환값에서 `id` (event_id) 추출.

**Step 3-B. 모든 이벤트 ID 저장**
```
PATCH /tasks/1/external-ids
Body: {
  "google_event_ids": [
    {"block_id": <gcal_payloads[0].block_id>, "event_id": "<반환된 id>"},
    ...
  ]
}
```

---

## 3. 태스크 상태 변경 동기화

### 완료 처리
```
PATCH /tasks/1/complete
```
이후 Notion 업데이트:
```
도구: notion-update-page
파라미터:
  page_id:    <task.notion_page_id>
  command:    "update_properties"
  properties: {"Status": "Done", "Completed At": "<오늘 날짜 YYYY-MM-DD>"}
```

### 우선순위 변경
```
PATCH /tasks/1/priority
Body: {"priority": "critical"}
```
이후 Notion 업데이트:
```
도구: notion-update-page
파라미터:
  page_id:    <task.notion_page_id>
  command:    "update_properties"
  properties: {"Priority": "🔴 Critical", "Priority Score": <새 점수>}
```

---

## 4. 블록 재일정 후 Google Calendar 업데이트

```bash
python main.py reschedule 3 --date 2026-04-16
```

재일정 후 Google Calendar 반영:

**Step 1. 기존 이벤트 삭제**
```
도구: gcal_delete_event
파라미터:
  calendarId: <GOOGLE_CALENDAR_ID>
  eventId:    <기존 block.google_event_id>
```

**Step 2. 새 블록 페이로드 조회**
```
GET /tasks/<task_id>/mcp-payload
```
새로 생성된 블록의 `gcal_payloads` 항목에서 `existing_event_id == null`인 것 찾기.

**Step 3. 새 이벤트 생성 후 ID 저장** (2-A~3-B와 동일)

---

## 5. 구글 캘린더 빈 시간 → 가용 시간 동기화

사용자가 "이번 주 구글 캘린더 빈 시간 가져와서 일정 업데이트해줘"라고 요청하면:

**Step 1. 빈 시간 조회**
```
도구: gcal_find_my_free_time
파라미터:
  calendarIds: ["primary"]
  timeMin:     "<오늘 날짜>T00:00:00"
  timeMax:     "<7일 후>T23:59:59"
  timeZone:    "Asia/Seoul"
  minDuration: 60
```

**Step 2. 파싱 후 저장**

반환된 `free_slots`를 날짜별 합산 후:
```
POST /schedule/capacity-from-gcal
Body: {
  "free_slots": [
    {"date": "2026-04-11", "free_hours": 5.5},
    {"date": "2026-04-12", "free_hours": 3.0},
    ...
  ]
}
```

---

## 6. Notion에서 태스크 임포트

사용자가 "노션에 있는 '마케팅 캠페인' 태스크 가져와줘"라고 요청하면:

**Step 1. Notion 검색**
```
도구: notion-search
파라미터:
  query:      "마케팅 캠페인"
  query_type: "internal"
```

**Step 2. 상세 조회 (필요 시)**
```
도구: notion-fetch
파라미터:
  id: <검색 결과의 page id>
```

**Step 3. 로컬 DB에 저장**
```
POST /tasks
Body: {
  "title":         "<Notion 페이지 제목>",
  "description":   "<설명>",
  "deadline":      "<마감일>",
  "priority":      "<우선순위>",
  "auto_breakdown": true,
  "sync_notion":   false   ← 이미 Notion에 있으므로 false
}
```

저장 후:
```
PATCH /tasks/<new_id>/external-ids
Body: {"notion_page_id": "<기존 Notion page id>"}
```

---

## 7. 부하 분석 후 일정 조정

사용자가 "이번 주 너무 바쁜데 일정 조정해줘"라고 요청하면:

**Step 1. 부하 분석**
```
GET /workload
```
AI 분석 결과의 `overloaded_days`, `priority_adjustments` 확인.

**Step 2. 우선순위 조정**
```
PATCH /tasks/<id>/priority
Body: {"priority": "low"}
```

**Step 3. 과부하 블록 이월**
```
POST /schedule/blocks/<block_id>/reschedule
Body: {"to_date": "2026-04-18", "reason": "과부하 조정"}
```

**Step 4. Google Calendar 업데이트** (섹션 4 참고)

---

## 8. 주요 API 레퍼런스

| Method | Path | 설명 |
|--------|------|------|
| GET | `/tasks/{id}/mcp-payload` | MCP 호출용 페이로드 반환 |
| PATCH | `/tasks/{id}/external-ids` | 외부 ID (Notion page_id, Google event_id) 저장 |
| POST | `/schedule/capacity-from-gcal` | gcal 빈 시간으로 CapacityLog 업데이트 |
| GET | `/capacity?days=7` | 7일 가용 시간 현황 |
| GET | `/workload` | AI 부하 분석 |
| GET | `/schedule/week` | 주간 블록 목록 |
| POST | `/schedule/blocks/{id}/reschedule` | 블록 이월 |

서버 실행: `python main.py serve`  
API 문서: `http://localhost:8000/docs`
