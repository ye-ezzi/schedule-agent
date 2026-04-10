"""알림 발송 시스템.

지원 채널:
  - desktop: plyer 데스크톱 알림 (macOS/Windows/Linux)
  - slack:   Slack Incoming Webhook
  - webhook: 커스텀 HTTP POST
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import httpx
import pytz
from sqlalchemy.orm import Session

from config import settings
from models.task import BlockStatus, NotificationLog, ScheduleBlock, Task, TaskStatus

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, session: Session):
        self.session = session
        self.tz = pytz.timezone(settings.timezone)

    # ─── 공개 인터페이스 ────────────────────────────────────────────────────────

    def notify_missed_block(self, block: ScheduleBlock) -> None:
        """미완료 블록 알림 + 재일정 제안."""
        task = block.task
        now = datetime.now(self.tz)
        overdue_hours = round((now - block.end_time.astimezone(self.tz)).total_seconds() / 3600, 1)

        msg = (
            f"⚠️ 미완료 작업 알림\n\n"
            f"작업: {task.title}\n"
            f"예정 시간: {block.start_time.astimezone(self.tz).strftime('%m/%d %H:%M')} ~ "
            f"{block.end_time.astimezone(self.tz).strftime('%H:%M')}\n"
            f"초과 시간: {overdue_hours}시간\n"
            f"이월 횟수: {task.carry_over_count}회\n\n"
            f"👉 /reschedule {block.id} 로 일정을 변경하세요"
        )

        self._send(
            title="⚠️ 미완료 작업",
            message=msg,
            notification_type="missed",
            task_id=task.id,
            block_id=block.id,
        )

    def notify_overdue_task(self, task: Task) -> None:
        """마감일 초과 알림."""
        if not task.deadline:
            return
        now = datetime.now(self.tz)
        overdue_days = (now - task.deadline.astimezone(self.tz)).days

        msg = (
            f"🚨 마감 초과!\n\n"
            f"작업: {task.title}\n"
            f"마감일: {task.deadline.astimezone(self.tz).strftime('%Y/%m/%d')}\n"
            f"초과일: {overdue_days}일\n"
            f"진행률: {task.progress_pct}%\n"
            f"이월 횟수: {task.carry_over_count}회"
        )

        self._send(
            title="🚨 마감 초과",
            message=msg,
            notification_type="overdue",
            task_id=task.id,
        )

    def send_daily_summary(self) -> None:
        """오늘 일정 요약 알림 (매일 아침)."""
        from models.task import ScheduleBlock

        today = datetime.now(self.tz).date()
        start = datetime(today.year, today.month, today.day, tzinfo=self.tz)
        end = start.replace(hour=23, minute=59, second=59)

        blocks = (
            self.session.query(ScheduleBlock)
            .filter(
                ScheduleBlock.start_time >= start,
                ScheduleBlock.start_time <= end,
                ScheduleBlock.status == BlockStatus.SCHEDULED,
            )
            .order_by(ScheduleBlock.start_time)
            .all()
        )

        if not blocks:
            self._send(
                title="📅 오늘 일정",
                message="오늘 예정된 작업이 없습니다. 여유 시간을 활용해 밀린 작업을 처리해보세요!",
                notification_type="daily_summary",
            )
            return

        total_hours = sum(b.planned_hours for b in blocks)
        task_lines = []
        for b in blocks:
            task = b.task
            time_str = b.start_time.astimezone(self.tz).strftime("%H:%M")
            task_lines.append(f"  {time_str} • {task.title} ({b.planned_hours}h)")

        msg = (
            f"📅 오늘의 일정 ({today.strftime('%m/%d')})\n\n"
            + "\n".join(task_lines)
            + f"\n\n총 작업 시간: {total_hours}h"
        )

        self._send(
            title="📅 오늘의 일정",
            message=msg,
            notification_type="daily_summary",
        )

    def send_reschedule_suggestion(
        self,
        block: ScheduleBlock,
        suggestion: dict,
    ) -> None:
        """AI 재일정 제안 알림."""
        msg = (
            f"🔄 재일정 제안\n\n"
            f"작업: {block.task.title}\n"
            f"제안 날짜: {suggestion.get('recommended_slot', {}).get('date', '미정')}\n"
            f"이유: {suggestion.get('recommended_slot', {}).get('reason', '')}\n\n"
            f"{suggestion.get('message', '')}"
        )
        self._send(
            title="🔄 재일정 제안",
            message=msg,
            notification_type="reschedule",
            block_id=block.id,
            task_id=block.task_id,
        )

    def acknowledge(self, notification_id: int, response: Optional[dict] = None) -> None:
        """알림 확인 처리."""
        log = self.session.get(NotificationLog, notification_id)
        if log:
            log.acknowledged = True
            log.acknowledged_at = datetime.now(self.tz)
            if response:
                log.user_response = json.dumps(response, ensure_ascii=False)
            self.session.flush()

    # ─── 내부 발송 ─────────────────────────────────────────────────────────────

    def _send(
        self,
        title: str,
        message: str,
        notification_type: str,
        task_id: Optional[int] = None,
        block_id: Optional[int] = None,
    ) -> None:
        channel = settings.notification_method
        success = False

        try:
            if channel == "desktop":
                success = self._send_desktop(title, message)
            elif channel == "slack":
                success = self._send_slack(title, message)
            elif channel == "webhook":
                success = self._send_webhook(title, message, notification_type, task_id, block_id)
            else:
                logger.warning(f"Unknown notification channel: {channel}")
        except Exception as e:
            logger.error(f"Notification failed [{channel}]: {e}")

        # 발송 이력 저장
        log = NotificationLog(
            task_id=task_id,
            block_id=block_id,
            notification_type=notification_type,
            message=message,
            channel=channel,
        )
        self.session.add(log)
        self.session.flush()

        if success:
            logger.info(f"Notification sent [{channel}]: {title}")

    def _send_desktop(self, title: str, message: str) -> bool:
        """데스크톱 알림 (plyer)."""
        try:
            from plyer import notification
            notification.notify(
                title=title,
                message=message[:256],  # plyer 길이 제한
                app_name="Schedule Agent",
                timeout=10,
            )
            return True
        except Exception as e:
            logger.error(f"Desktop notification failed: {e}")
            return False

    def _send_slack(self, title: str, message: str) -> bool:
        """Slack Incoming Webhook."""
        if not settings.slack_webhook_url:
            logger.warning("Slack webhook URL not configured.")
            return False
        payload = {
            "text": f"*{title}*\n{message}",
            "mrkdwn": True,
        }
        resp = httpx.post(settings.slack_webhook_url, json=payload, timeout=5)
        resp.raise_for_status()
        return True

    def _send_webhook(
        self,
        title: str,
        message: str,
        notification_type: str,
        task_id: Optional[int],
        block_id: Optional[int],
    ) -> bool:
        """커스텀 HTTP Webhook."""
        if not settings.webhook_notify_url:
            logger.warning("Webhook URL not configured.")
            return False
        payload = {
            "title": title,
            "message": message,
            "type": notification_type,
            "task_id": task_id,
            "block_id": block_id,
            "timestamp": datetime.now(self.tz).isoformat(),
        }
        resp = httpx.post(settings.webhook_notify_url, json=payload, timeout=5)
        resp.raise_for_status()
        return True
