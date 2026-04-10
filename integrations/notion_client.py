"""Notion API 연동: 태스크를 Notion 데이터베이스에 동기화."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pytz
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

from config import settings
from models.task import Priority, Task, TaskStatus

logger = logging.getLogger(__name__)

# Notion 우선순위 레이블 매핑
PRIORITY_LABEL = {
    Priority.CRITICAL: "🔴 Critical",
    Priority.HIGH: "🟠 High",
    Priority.MEDIUM: "🟡 Medium",
    Priority.LOW: "🟢 Low",
}

STATUS_LABEL = {
    TaskStatus.PENDING: "Not Started",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.COMPLETED: "Done",
    TaskStatus.DEFERRED: "Deferred",
    TaskStatus.CANCELLED: "Cancelled",
}


class NotionIntegration:
    """Notion 데이터베이스에 태스크를 읽고 씁니다."""

    def __init__(self):
        if not settings.notion_api_key:
            raise ValueError("NOTION_API_KEY가 설정되지 않았습니다.")
        self.client = NotionClient(auth=settings.notion_api_key)
        self.db_id = settings.notion_tasks_database_id
        self.tz = pytz.timezone(settings.timezone)

    # ─── 태스크 → Notion ───────────────────────────────────────────────────────

    def create_page(self, task: Task) -> str:
        """Task를 Notion 페이지로 생성. 생성된 page_id 반환."""
        props = self._build_properties(task)
        children = self._build_children(task)

        page = self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=props,
            children=children,
        )
        page_id = page["id"]
        logger.info(f"Notion page created: {page_id} for task '{task.title}'")
        return page_id

    def update_page(self, task: Task) -> None:
        """Task 상태 변경을 Notion에 반영."""
        if not task.notion_page_id:
            return
        try:
            self.client.pages.update(
                page_id=task.notion_page_id,
                properties=self._build_properties(task),
            )
        except APIResponseError as e:
            logger.error(f"Notion update failed: {e}")

    def complete_page(self, task: Task) -> None:
        """Notion 페이지를 완료 상태로 변경."""
        if not task.notion_page_id:
            return
        try:
            self.client.pages.update(
                page_id=task.notion_page_id,
                properties={
                    "Status": {"select": {"name": STATUS_LABEL[TaskStatus.COMPLETED]}},
                    "Completed At": {"date": {"start": datetime.now(self.tz).isoformat()}},
                },
            )
        except APIResponseError as e:
            logger.error(f"Notion complete failed: {e}")

    # ─── Notion → 태스크 ───────────────────────────────────────────────────────

    def fetch_pages(self, filter_status: Optional[str] = None) -> list[dict]:
        """Notion DB에서 페이지 목록을 가져온다."""
        f = {}
        if filter_status:
            f = {
                "property": "Status",
                "select": {"equals": filter_status},
            }
        results = []
        cursor = None
        while True:
            kwargs = {
                "database_id": self.db_id,
                "sorts": [{"property": "Deadline", "direction": "ascending"}],
            }
            if f:
                kwargs["filter"] = f
            if cursor:
                kwargs["start_cursor"] = cursor

            resp = self.client.databases.query(**kwargs)
            results.extend(resp.get("results", []))
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        return results

    def page_to_task_data(self, page: dict) -> dict:
        """Notion 페이지 → Task 생성용 dict 변환."""
        props = page.get("properties", {})

        title = ""
        if props.get("Name"):
            rich = props["Name"].get("title", [])
            title = "".join(r.get("plain_text", "") for r in rich)

        deadline = None
        if props.get("Deadline", {}).get("date", {}).get("start"):
            dl_str = props["Deadline"]["date"]["start"]
            deadline = datetime.fromisoformat(dl_str).replace(tzinfo=self.tz)

        status_label = props.get("Status", {}).get("select", {}).get("name", "Not Started")
        status = {v: k for k, v in STATUS_LABEL.items()}.get(status_label, TaskStatus.PENDING)

        priority_label = props.get("Priority", {}).get("select", {}).get("name", "🟡 Medium")
        priority = {v: k for k, v in PRIORITY_LABEL.items()}.get(priority_label, Priority.MEDIUM)

        description = ""
        if props.get("Description", {}).get("rich_text"):
            description = "".join(
                r.get("plain_text", "")
                for r in props["Description"]["rich_text"]
            )

        return {
            "title": title,
            "description": description,
            "deadline": deadline,
            "status": status,
            "priority": priority,
            "notion_page_id": page["id"],
        }

    # ─── 헬퍼 ──────────────────────────────────────────────────────────────────

    def _build_properties(self, task: Task) -> dict:
        props: dict = {
            "Name": {"title": [{"text": {"content": task.title}}]},
            "Status": {"select": {"name": STATUS_LABEL.get(task.status, "Not Started")}},
            "Priority": {"select": {"name": PRIORITY_LABEL.get(task.priority, "🟡 Medium")}},
        }
        if task.deadline:
            props["Deadline"] = {"date": {"start": task.deadline.isoformat()}}
        if task.estimated_hours:
            props["Estimated Hours"] = {"number": task.estimated_hours}
        if task.description:
            props["Description"] = {
                "rich_text": [{"text": {"content": task.description[:2000]}}]
            }
        if task.priority_score:
            props["Priority Score"] = {"number": task.priority_score}
        return props

    def _build_children(self, task: Task) -> list:
        """Notion 페이지 본문 블록 생성 (서브태스크 체크리스트)."""
        children = []

        if task.subtasks:
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "📋 세부 작업"}}]},
            })
            for st in task.subtasks:
                children.append({
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [
                            {"text": {"content": f"{st.title} ({st.estimated_hours}h)"}}
                        ],
                        "checked": st.status == TaskStatus.COMPLETED,
                    },
                })

        if task.description and "[AI 분석]" in task.description:
            parts = task.description.split("[AI 분석]")
            ai_notes = parts[1].strip() if len(parts) > 1 else ""
            if ai_notes:
                children.append({
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {"rich_text": [{"text": {"content": "🤖 AI 분석"}}]},
                })
                children.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": ai_notes[:2000]}}]},
                })

        return children
