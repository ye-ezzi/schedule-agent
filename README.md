# Schedule Agent

Notion / Google Calendar / Apple Calendar 통합 AI 스케줄 관리 에이전트

---

## 주요 기능

| 기능 | 설명 |
|------|------|
| **AI 태스크 분해** | Claude AI가 업무를 분석해 세부 작업·예상 시간·일정 플랜 자동 생성 |
| **우선순위 엔진** | 마감 임박도 + 명시 우선순위 + 이월 횟수로 점수 산정, 자동 정렬 |
| **가용 시간 관리** | 날짜별 업무 가능 시간 설정, 초과 배정 방지 |
| **자동 이월** | 미완료 블록을 자정에 다음 가용 슬롯으로 자동 이월 |
| **3시간 알림** | 미완료 항목을 3시간마다 체크해 알림 발송 (Desktop / Slack / Webhook) |
| **Notion 동기화** | 태스크를 Notion DB 페이지로 동기화 (서브태스크 체크리스트 포함) |
| **Google Calendar** | ScheduleBlock을 Google Calendar 이벤트로 동기화 |
| **Apple Calendar** | iCloud CalDAV를 통해 Apple Calendar에 이벤트 동기화 |

---

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env
# .env 파일에서 API 키 설정
```

---

## 빠른 시작

### 태스크 추가 (AI 분해 포함)
```bash
python main.py add "앱 랜딩 페이지 디자인" \
  --deadline 2026-04-20 \
  --priority high \
  --notion \
  --google
```

### 오늘 할 일 확인
```bash
python main.py today
```

### 전체 태스크 목록
```bash
python main.py list
python main.py list --status pending
python main.py list --priority critical
```

### 일정 블록 조회
```bash
python main.py schedule          # 7일치
python main.py schedule --days 14
```

### 가용 시간 현황
```bash
python main.py capacity
```

### 태스크 완료
```bash
python main.py complete 1
python main.py complete 1 --hours 2.5   # 실제 소요 시간 기록
```

### 블록 수동 이월
```bash
python main.py reschedule 3                      # 다음 날로
python main.py reschedule 3 --date 2026-04-15    # 특정 날로
python main.py reschedule 3 -r "회의로 인한 이월"
```

### 서버 실행 (API + 스케줄러)
```bash
python main.py serve
# API 문서: http://localhost:8000/docs
```

---

## 환경 변수 (.env)

### 필수
```
ANTHROPIC_API_KEY=sk-ant-...
```

### Notion 연동
```
NOTION_API_KEY=secret_...
NOTION_TASKS_DATABASE_ID=<DB ID>
```

Notion 데이터베이스 필수 속성:
- `Name` (title)
- `Status` (select): Not Started / In Progress / Done / Deferred / Cancelled
- `Priority` (select): 🔴 Critical / 🟠 High / 🟡 Medium / 🟢 Low
- `Deadline` (date)
- `Estimated Hours` (number)
- `Description` (rich_text)
- `Priority Score` (number)
- `Completed At` (date)

### Google Calendar 연동
```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_CALENDAR_ID=primary
```

1. Google Cloud Console에서 OAuth 2.0 클라이언트 생성
2. `google_credentials.json` 다운로드
3. `python main.py serve` 후 브라우저에서 `http://localhost:8000/auth/google`

### Apple Calendar 연동
```
APPLE_CALDAV_URL=https://caldav.icloud.com/
APPLE_CALDAV_USERNAME=your@icloud.com
APPLE_CALDAV_PASSWORD=앱-전용-비밀번호
APPLE_CALENDAR_NAME=schedule-agent
```

[앱 전용 비밀번호 생성](https://appleid.apple.com) → 로그인 및 보안 → 앱 전용 암호

### 알림 설정
```
NOTIFICATION_INTERVAL_HOURS=3
NOTIFICATION_METHOD=desktop     # desktop | slack | webhook
SLACK_WEBHOOK_URL=              # slack 선택 시
WEBHOOK_NOTIFY_URL=             # webhook 선택 시
```

### 업무 시간
```
TIMEZONE=Asia/Seoul
WORK_START_HOUR=9
WORK_END_HOUR=22
DAILY_CAPACITY_HOURS=8
```

---

## REST API

서버 실행 후 `http://localhost:8000/docs`에서 Swagger UI 확인

| Method | Path | 설명 |
|--------|------|------|
| POST | /tasks | 태스크 생성 |
| GET | /tasks | 목록 조회 |
| GET | /tasks/today | 오늘 태스크 |
| PATCH | /tasks/{id}/complete | 완료 처리 |
| PATCH | /tasks/{id}/priority | 우선순위 변경 |
| GET | /schedule/today | 오늘 블록 |
| GET | /schedule/week | 주간 블록 |
| POST | /schedule/blocks/{id}/reschedule | 블록 재일정 |
| GET | /capacity | 가용 시간 조회 |
| POST | /capacity/{date} | 가용 시간 설정 |
| GET | /workload | AI 부하 분석 |
| GET | /auth/google | Google 인증 URL |
| GET | /notifications | 알림 목록 |
| POST | /notifications/{id}/ack | 알림 확인 |

---

## 아키텍처

```
schedule-agent/
├── main.py                  # CLI (typer + rich)
├── config.py                # 환경 설정
├── models/task.py           # SQLAlchemy ORM (Task, SubTask, ScheduleBlock, ...)
├── db/database.py           # SQLite 세션 관리
├── ai/task_breakdown.py     # Claude API - 태스크 분해/재일정/부하 분석
├── core/
│   ├── task_manager.py      # 태스크 CRUD + AI 연동
│   ├── capacity_planner.py  # 가용 시간 계산 + 블록 배정
│   ├── priority_engine.py   # 우선순위 점수 엔진
│   ├── carryover.py         # 이월 서비스
│   └── scheduler.py         # APScheduler (3h알림 + 자정이월 + 아침요약)
├── integrations/
│   ├── notion_client.py     # Notion API
│   ├── google_calendar.py   # Google Calendar API
│   └── apple_calendar.py    # Apple iCloud CalDAV
├── notifications/notifier.py # Desktop / Slack / Webhook 알림
└── api/server.py            # FastAPI REST API
```

---

## 추가 고려 사항

- **반복 일정**: 매주 정기 미팅 등 반복 태스크 지원 (향후)
- **팀 공유**: 다중 사용자 지원 (향후)
- **에너지 레벨**: 오전/오후 집중력 패턴 학습 (향후)
- **Notion 임포트**: 기존 Notion DB에서 태스크 가져오기 (향후)
- **Time tracking**: 실제 작업 시간 타이머 (향후)
