"""APScheduler 기반 백그라운드 스케줄러.

담당 작업:
  1. 3시간마다 - 미완료 블록 체크 & 알림
  2. 매일 자정  - 이월 처리 (carryover)
  3. 매일 오전  - 오늘 일정 요약 알림
"""
from __future__ import annotations

import logging
from datetime import datetime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(self):
        self.tz = pytz.timezone(settings.timezone)
        self.scheduler = BackgroundScheduler(timezone=self.tz)
        self._setup_jobs()

    def _setup_jobs(self):
        # 1. 3시간마다 미완료 블록 알림
        self.scheduler.add_job(
            self._check_missed_blocks,
            trigger=IntervalTrigger(hours=settings.notification_interval_hours),
            id="check_missed_blocks",
            replace_existing=True,
            misfire_grace_time=300,
        )

        # 2. 매일 자정 이월 처리
        self.scheduler.add_job(
            self._daily_carryover,
            trigger=CronTrigger(hour=0, minute=5, timezone=self.tz),
            id="daily_carryover",
            replace_existing=True,
        )

        # 3. 매일 오전 9시 오늘 일정 요약
        self.scheduler.add_job(
            self._morning_summary,
            trigger=CronTrigger(
                hour=settings.work_start_hour,
                minute=0,
                timezone=self.tz,
            ),
            id="morning_summary",
            replace_existing=True,
        )

    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    # ─── Job 함수 ──────────────────────────────────────────────────────────────

    def _check_missed_blocks(self):
        """3시간마다: 지나간 블록 중 미완료 항목 알림."""
        from db.database import get_session
        from notifications.notifier import Notifier
        from models.task import BlockStatus, ScheduleBlock

        now = datetime.now(self.tz)
        logger.info(f"[{now}] Checking missed blocks...")

        with get_session() as session:
            missed = (
                session.query(ScheduleBlock)
                .filter(
                    ScheduleBlock.end_time < now,
                    ScheduleBlock.status.in_([BlockStatus.SCHEDULED, BlockStatus.ACTIVE]),
                    ScheduleBlock.notification_sent == False,
                )
                .all()
            )

            if not missed:
                logger.info("No missed blocks.")
                return

            notifier = Notifier(session)
            for block in missed:
                notifier.notify_missed_block(block)
                block.notification_sent = True
                block.notification_sent_at = now
                session.flush()

            logger.info(f"Notified {len(missed)} missed block(s).")

    def _daily_carryover(self):
        """매일 자정: 미완료 작업 이월."""
        from db.database import get_session
        from core.carryover import CarryoverService

        logger.info("Running daily carryover...")
        with get_session() as session:
            svc = CarryoverService(session)
            result = svc.run_daily_carryover()
            logger.info(f"Carryover result: {result['carried_over']} tasks carried over.")

    def _morning_summary(self):
        """매일 아침: 오늘 할 일 요약 알림."""
        from db.database import get_session
        from notifications.notifier import Notifier

        logger.info("Sending morning summary...")
        with get_session() as session:
            notifier = Notifier(session)
            notifier.send_daily_summary()
