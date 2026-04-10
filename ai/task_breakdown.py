"""Claude AI를 활용한 태스크 분해 및 일정 추천 엔진."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

import anthropic
import pytz

from config import settings


# ─── Pydantic-style response schemas (dicts) ──────────────────────────────────

BREAKDOWN_SYSTEM_PROMPT = """당신은 전문적인 프로젝트 매니저이자 생산성 코치입니다.
사용자가 제시한 업무/프로젝트를 분석하여 실행 가능한 세부 작업으로 분해하고,
각 작업의 소요 시간을 현실적으로 예측하며, 우선순위를 설정합니다.

응답은 반드시 아래 JSON 형식으로만 답변하세요 (마크다운 코드블록 없이 순수 JSON):
{
  "analysis": "업무 전체 분석 (1-2문장)",
  "total_estimated_hours": 숫자,
  "recommended_priority": "critical|high|medium|low",
  "risk_factors": ["위험 요소1", "위험 요소2"],
  "considerations": ["추가 고려 사항1", "추가 고려 사항2"],
  "subtasks": [
    {
      "title": "세부 작업 제목",
      "description": "상세 설명",
      "estimated_hours": 숫자,
      "order": 순서(1부터),
      "checklist": ["체크리스트 항목1", "항목2"],
      "dependencies": ["선행되어야 할 다른 subtask title (없으면 빈 배열)"]
    }
  ],
  "daily_plan": [
    {
      "day_offset": 0,
      "suggested_hours": 숫자,
      "subtask_titles": ["이 날 처리할 subtask 제목들"]
    }
  ]
}

주의사항:
- subtasks는 순서대로 나열 (의존성 고려)
- estimated_hours는 집중 작업 기준 (회의, 이동 등 제외)
- 하루 최대 집중 작업 시간: 6시간 기준으로 daily_plan 구성
- 버퍼 시간(예상의 20%)을 포함하여 계산
"""


class TaskBreakdownEngine:
    """Anthropic Claude를 사용해 태스크를 분해하고 일정을 추천합니다."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.tz = pytz.timezone(settings.timezone)

    def breakdown(
        self,
        title: str,
        description: str = "",
        deadline: Optional[datetime] = None,
        available_hours_per_day: float = settings.daily_capacity_hours,
        existing_tasks_summary: str = "",
    ) -> dict:
        """
        태스크를 AI로 분석·분해한다.

        Returns:
            {
                analysis, total_estimated_hours, recommended_priority,
                risk_factors, considerations, subtasks, daily_plan
            }
        """
        deadline_str = ""
        if deadline:
            now = datetime.now(self.tz)
            days_left = (deadline.astimezone(self.tz) - now).days
            deadline_str = f"\n마감일: {deadline.strftime('%Y-%m-%d %H:%M')} (오늘로부터 {days_left}일 후)"

        existing_str = ""
        if existing_tasks_summary:
            existing_str = f"\n\n현재 진행 중인 다른 작업들:\n{existing_tasks_summary}"

        user_message = f"""다음 업무를 분석하고 세부 작업으로 분해해주세요.

업무명: {title}
상세 내용: {description or '(없음)'}
하루 가용 시간: {available_hours_per_day}시간{deadline_str}{existing_str}

위 업무를 실행 가능한 세부 작업들로 분해하고, 현실적인 일정 계획을 제안해주세요."""

        message = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": user_message}],
            system=BREAKDOWN_SYSTEM_PROMPT,
        )

        raw = message.content[0].text.strip()
        # JSON 파싱 (앞뒤 공백·코드블록 제거)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        return result

    def suggest_reschedule(
        self,
        missed_block_title: str,
        missed_time: datetime,
        remaining_hours: float,
        available_slots: list[dict],
    ) -> dict:
        """
        미완료 블록에 대해 재일정을 추천한다.

        available_slots: [{"date": "YYYY-MM-DD", "free_hours": float}]
        Returns: {"recommended_slot": {...}, "message": str}
        """
        slots_str = "\n".join(
            f"- {s['date']}: 여유 {s['free_hours']}시간" for s in available_slots[:7]
        )
        message = self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"작업 '{missed_block_title}'이 {missed_time.strftime('%m/%d %H:%M')}에 "
                        f"완료되지 않았습니다. 남은 예상 시간: {remaining_hours}시간\n\n"
                        f"이후 7일간 가용 슬롯:\n{slots_str}\n\n"
                        "가장 적합한 재일정 슬롯과 이유를 JSON으로 답하세요:\n"
                        '{"recommended_slot": {"date": "YYYY-MM-DD", "reason": "이유"}, "message": "사용자에게 보낼 메시지"}'
                    ),
                }
            ],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)

    def analyze_workload(
        self,
        tasks_summary: list[dict],
        capacity_summary: list[dict],
    ) -> dict:
        """
        전체 작업 부하를 분석하고 우선순위 조정을 추천한다.

        tasks_summary: [{"title", "priority", "deadline", "estimated_hours", "status"}]
        capacity_summary: [{"date", "available_hours", "scheduled_hours"}]
        Returns: {"overloaded_days": [...], "recommendations": [...], "priority_adjustments": [...]}
        """
        tasks_str = json.dumps(tasks_summary, ensure_ascii=False, indent=2)
        capacity_str = json.dumps(capacity_summary, ensure_ascii=False, indent=2)

        message = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"현재 작업 목록:\n{tasks_str}\n\n"
                        f"7일간 가용 시간:\n{capacity_str}\n\n"
                        "작업 부하를 분석하고 최적 일정을 추천해주세요. "
                        "JSON 형식으로:\n"
                        '{"overloaded_days": ["날짜들"], '
                        '"recommendations": ["추천사항들"], '
                        '"priority_adjustments": [{"task_title": "...", "suggested_priority": "...", "reason": "..."}]}'
                    ),
                }
            ],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
