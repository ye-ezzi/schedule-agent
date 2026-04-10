"""Google Calendar API 연동."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from models.task import ScheduleBlock, Task

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = Path(".google_token.json")
CREDENTIALS_FILE = Path("google_credentials.json")


class GoogleCalendarIntegration:
    """Google Calendar에 ScheduleBlock을 이벤트로 동기화합니다."""

    def __init__(self):
        self.tz = pytz.timezone(settings.timezone)
        self.calendar_id = settings.google_calendar_id
        self._service = None

    @property
    def service(self):
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        creds = None
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        if not creds or not creds.valid:
            raise RuntimeError(
                "Google OAuth 인증이 필요합니다. /auth/google 엔드포인트를 통해 인증하세요."
            )
        return build("calendar", "v3", credentials=creds)

    # ─── OAuth 흐름 ────────────────────────────────────────────────────────────

    def get_auth_url(self) -> str:
        """OAuth 인증 URL 반환."""
        if not CREDENTIALS_FILE.exists():
            raise FileNotFoundError(
                "google_credentials.json 파일이 없습니다. "
                "Google Cloud Console에서 다운로드하세요."
            )
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            scopes=SCOPES,
            redirect_uri=settings.google_redirect_uri,
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return auth_url

    def handle_callback(self, code: str) -> None:
        """OAuth 콜백 처리 및 토큰 저장."""
        flow = Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            scopes=SCOPES,
            redirect_uri=settings.google_redirect_uri,
        )
        flow.fetch_token(code=code)
        TOKEN_FILE.write_text(flow.credentials.to_json())
        logger.info("Google OAuth token saved.")

    # ─── 이벤트 생성/수정/삭제 ─────────────────────────────────────────────────

    def create_event(self, block: ScheduleBlock, task: Task) -> str:
        """ScheduleBlock → Google Calendar 이벤트 생성. event_id 반환."""
        event = self._block_to_event(block, task)
        try:
            result = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event,
            ).execute()
            event_id = result["id"]
            logger.info(f"Google event created: {event_id}")
            return event_id
        except HttpError as e:
            logger.error(f"Google Calendar create failed: {e}")
            raise

    def update_event(self, block: ScheduleBlock, task: Task) -> None:
        """이벤트 업데이트 (시간 변경, 상태 변경 등)."""
        if not block.google_event_id:
            return
        event = self._block_to_event(block, task)
        try:
            self.service.events().update(
                calendarId=self.calendar_id,
                eventId=block.google_event_id,
                body=event,
            ).execute()
        except HttpError as e:
            logger.error(f"Google Calendar update failed: {e}")

    def delete_event(self, event_id: str) -> None:
        """이벤트 삭제."""
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
            ).execute()
        except HttpError as e:
            logger.error(f"Google Calendar delete failed: {e}")

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        """기간 내 이벤트 목록 조회."""
        try:
            result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            return result.get("items", [])
        except HttpError as e:
            logger.error(f"Google Calendar list failed: {e}")
            return []

    # ─── 헬퍼 ──────────────────────────────────────────────────────────────────

    def _block_to_event(self, block: ScheduleBlock, task: Task) -> dict:
        priority_emoji = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }.get(task.priority.value, "📌")

        status_label = {
            "scheduled": "",
            "active": "▶️ ",
            "done": "✅ ",
            "missed": "⚠️ ",
            "rescheduled": "🔄 ",
        }.get(block.status.value, "")

        summary = f"{status_label}{priority_emoji} {task.title}"
        if block.planned_hours:
            summary += f" ({block.planned_hours}h)"

        description_parts = []
        if task.description:
            description_parts.append(task.description[:500])
        if task.subtasks:
            pending = [s.title for s in task.subtasks if s.status.value != "completed"]
            if pending:
                description_parts.append("남은 세부 작업:\n" + "\n".join(f"• {t}" for t in pending))
        if block.reschedule_count > 0:
            description_parts.append(f"이월 횟수: {block.reschedule_count}회")

        return {
            "summary": summary,
            "description": "\n\n".join(description_parts),
            "start": {
                "dateTime": block.start_time.isoformat(),
                "timeZone": settings.timezone,
            },
            "end": {
                "dateTime": block.end_time.isoformat(),
                "timeZone": settings.timezone,
            },
            "colorId": self._priority_color(task.priority.value),
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 30},
                    {"method": "popup", "minutes": 10},
                ],
            },
            "extendedProperties": {
                "private": {
                    "schedule_agent_block_id": str(block.id),
                    "schedule_agent_task_id": str(task.id),
                }
            },
        }

    @staticmethod
    def _priority_color(priority: str) -> str:
        # Google Calendar color IDs (1-11)
        return {"critical": "11", "high": "6", "medium": "5", "low": "2"}.get(priority, "1")
