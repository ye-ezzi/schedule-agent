from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str = ""

    # Notion
    notion_api_key: str = ""
    notion_tasks_database_id: str = ""
    notion_projects_database_id: str = ""

    # Google Calendar
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_calendar_id: str = "primary"

    # Apple CalDAV
    apple_caldav_url: str = "https://caldav.icloud.com/"
    apple_caldav_username: str = ""
    apple_caldav_password: str = ""
    apple_calendar_name: str = "schedule-agent"

    # Notifications
    notification_interval_hours: int = 3
    notification_method: str = "desktop"  # desktop | slack | webhook
    slack_webhook_url: str = ""
    webhook_notify_url: str = ""

    # App
    database_url: str = "sqlite:///./schedule_agent.db"
    timezone: str = "Asia/Seoul"
    work_start_hour: int = 9
    work_end_hour: int = 22
    daily_capacity_hours: float = 8.0
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    debug: bool = False


settings = Settings()
