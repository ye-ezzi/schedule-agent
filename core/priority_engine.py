"""우선순위 점수 계산 엔진.

점수 산정 기준 (0 ~ 100):
  - 마감 임박도 (40점): 마감까지 남은 시간이 짧을수록 높음
  - 명시적 우선순위 (30점): critical > high > medium > low
  - 이월 횟수 (20점): 자주 밀릴수록 점수 증가 (최대 20점)
  - 작업량 부담 (10점): 작업이 가벼울수록 먼저 처리 유도
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytz

from config import settings
from models.task import Priority, Task


_PRIORITY_BASE = {
    Priority.CRITICAL: 30,
    Priority.HIGH: 22,
    Priority.MEDIUM: 14,
    Priority.LOW: 5,
}


class PriorityEngine:
    def __init__(self):
        self.tz = pytz.timezone(settings.timezone)

    def calculate_score(self, task: Task) -> float:
        """0~100 사이 점수 반환. 높을수록 먼저 처리."""
        score = 0.0

        # 1. 마감 임박도 (0~40)
        score += self._deadline_score(task.deadline)

        # 2. 명시적 우선순위 (0~30)
        score += _PRIORITY_BASE.get(task.priority, 14)

        # 3. 이월 횟수 패널티 → 오히려 점수 상승 (0~20)
        carry = min(task.carry_over_count, 5)
        score += carry * 4  # 최대 5회 × 4 = 20

        # 4. 작업 가벼움 보너스 (0~10)
        est = task.estimated_hours or 1.0
        if est <= 1.0:
            score += 10
        elif est <= 2.0:
            score += 7
        elif est <= 4.0:
            score += 4
        else:
            score += 1

        return round(min(score, 100.0), 2)

    def _deadline_score(self, deadline: Optional[datetime]) -> float:
        if not deadline:
            return 5.0  # 마감 없는 작업은 낮은 기본 점수
        now = datetime.now(self.tz)
        dl = deadline.astimezone(self.tz)
        hours_left = (dl - now).total_seconds() / 3600

        if hours_left <= 0:
            return 40.0          # 이미 지남
        elif hours_left <= 4:
            return 38.0
        elif hours_left <= 24:
            return 34.0
        elif hours_left <= 48:
            return 28.0
        elif hours_left <= 72:
            return 20.0
        elif hours_left <= 168:  # 1주일
            return 12.0
        elif hours_left <= 336:  # 2주일
            return 7.0
        else:
            return 3.0

    def sort_tasks(self, tasks: list[Task]) -> list[Task]:
        """우선순위 점수 기준 내림차순 정렬."""
        for t in tasks:
            t.priority_score = self.calculate_score(t)
        return sorted(tasks, key=lambda t: t.priority_score, reverse=True)

    def suggest_priority(self, deadline: Optional[datetime]) -> Priority:
        """마감일 기반으로 적절한 우선순위를 제안."""
        score = self._deadline_score(deadline)
        if score >= 35:
            return Priority.CRITICAL
        elif score >= 25:
            return Priority.HIGH
        elif score >= 10:
            return Priority.MEDIUM
        else:
            return Priority.LOW
