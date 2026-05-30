"""
Phase 6-3: Notification system — Telegram + Slack alerts for schema drift & relationship changes
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum

import httpx

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class DriftEvent:
    """스키마 드리프트 이벤트"""
    event_id: str
    timestamp: str
    severity: Severity
    db_name: str
    table_name: str
    change_type: str  # "column_added", "column_removed", "column_modified", "table_added", "table_removed"
    field_name: str = ""
    old_value: str = ""
    new_value: str = ""
    description: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class RelationshipChangeEvent:
    """관계 변경 이벤트"""
    event_id: str
    timestamp: str
    severity: Severity
    change_type: str  # "relationship_added", "relationship_removed", "confidence_changed"
    source_table: str = ""
    source_column: str = ""
    target_table: str = ""
    target_column: str = ""
    relationship_type: str = ""
    old_confidence: float = 0.0
    new_confidence: float = 0.0
    description: str = ""


class TelegramNotifier:
    """텔레그램 봇으로 알림 전송"""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 15):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.base_url = self.BASE_URL.format(token=bot_token)

    def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        try:
            resp = httpx.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    def send_drift_alert(self, event: DriftEvent) -> bool:
        emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(event.severity.value, "⚪")
        change_labels = {
            "column_added": "컬럼 추가",
            "column_removed": "컬럼 제거",
            "column_modified": "컬럼 변경",
            "table_added": "테이블 추가",
            "table_removed": "테이블 제거",
        }
        change_label = change_labels.get(event.change_type, event.change_type)
        lines = [
            f"{emoji} **[DB 스키마 드리프트 감지]**",
            "",
            f"시간: `{event.timestamp}`",
            f"DB: `{event.db_name}`",
            f"테이블: `{event.table_name}`",
            f"변경: **{change_label}**",
        ]
        if event.field_name:
            lines.append(f"필드: `{event.field_name}`")
        if event.old_value:
            lines.append(f"이전: `{event.old_value}`")
        if event.new_value:
            lines.append(f"변경: `{event.new_value}`")
        if event.description:
            lines.append(f"")
            lines.append(f"설명: {event.description}")
        return self.send_message("\n".join(lines))

    def send_relationship_alert(self, event: RelationshipChangeEvent) -> bool:
        emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(event.severity.value, "⚪")
        change_labels = {
            "relationship_added": "새 관계 발견",
            "relationship_removed": "관계 소멸",
            "confidence_changed": "신뢰도 변경",
        }
        lines = [
            f"{emoji} **[온톨로지 관계 변경 감지]**",
            "",
            f"시간: `{event.timestamp}`",
            f"변경: **{change_labels.get(event.change_type, event.change_type)}**",
            f"관계: `{event.relationship_type}`",
            f"`{event.source_table}.{event.source_column}` -> `{event.target_table}.{event.target_column}`",
        ]
        if event.change_type == "confidence_changed":
            lines.append(f"신뢰도: `{event.old_confidence:.2f}` -> `{event.new_confidence:.2f}`")
        if event.description:
            lines.append(f"설명: {event.description}")
        return self.send_message("\n".join(lines))


class SlackNotifier:
    """Slack Incoming Webhook으로 알림 전송"""

    def __init__(self, webhook_url: str, timeout: int = 15):
        self.webhook_url = webhook_url
        self.timeout = timeout

    def _post(self, payload: dict) -> bool:
        try:
            resp = httpx.post(self.webhook_url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Slack send failed: {e}")
            return False

    def send_drift_alert(self, event: DriftEvent) -> bool:
        color = {"critical": "#FF0000", "warning": "#FFA500", "info": "#36A64F"}.get(event.severity.value, "#808080")
        change_labels = {
            "column_added": "컬럼 추가",
            "column_removed": "컬럼 제거",
            "column_modified": "컬럼 변경",
            "table_added": "테이블 추가",
            "table_removed": "테이블 제거",
        }
        fields = [
            {"title": "DB", "value": event.db_name, "short": True},
            {"title": "테이블", "value": event.table_name, "short": True},
            {"title": "변경", "value": change_labels.get(event.change_type, event.change_type), "short": True},
        ]
        if event.field_name:
            fields.append({"title": "필드", "value": event.field_name, "short": True})
        if event.old_value:
            fields.append({"title": "이전", "value": event.old_value, "short": True})
        if event.new_value:
            fields.append({"title": "변경값", "value": event.new_value, "short": True})
        if event.description:
            fields.append({"title": "설명", "value": event.description, "short": False})
        payload = {
            "attachments": [{
                "color": color,
                "title": "DB 스키마 드리프트 감지",
                "fields": fields,
                "footer": f"event_id: {event.event_id}",
            }]
        }
        return self._post(payload)

    def send_relationship_alert(self, event: RelationshipChangeEvent) -> bool:
        color = {"critical": "#FF0000", "warning": "#FFA500", "info": "#36A64F"}.get(event.severity.value, "#808080")
        payload = {
            "attachments": [{
                "color": color,
                "title": "온톨로지 관계 변경 감지",
                "fields": [
                    {"title": "변경", "value": event.change_type, "short": True},
                    {"title": "관계", "value": event.relationship_type, "short": True},
                    {"title": "소스", "value": f"{event.source_table}.{event.source_column}", "short": True},
                    {"title": "타겟", "value": f"{event.target_table}.{event.target_column}", "short": True},
                ],
                "footer": f"event_id: {event.event_id}",
            }]
        }
        if event.change_type == "confidence_changed":
            payload["attachments"][0]["fields"].append({
                "title": "신뢰도",
                "value": f"{event.old_confidence:.2f} -> {event.new_confidence:.2f}",
                "short": True,
            })
        return self._post(payload)


class NotificationManager:
    """여러 알림 채널을 통합 관리"""

    def __init__(self):
        self._channels: list = []
        self._history: list[dict] = []

    def add_telegram(self, bot_token: str, chat_id: str) -> "NotificationManager":
        self._channels.append(TelegramNotifier(bot_token, chat_id))
        return self

    def add_slack(self, webhook_url: str) -> "NotificationManager":
        self._channels.append(SlackNotifier(webhook_url))
        return self

    def notify_drift(self, event: DriftEvent) -> dict[str, bool]:
        results = {}
        for ch in self._channels:
            if isinstance(ch, TelegramNotifier):
                results["telegram"] = ch.send_drift_alert(event)
            elif isinstance(ch, SlackNotifier):
                results["slack"] = ch.send_drift_alert(event)
        self._history.append({
            "type": "drift",
            "results": results,
            "timestamp": datetime.now().isoformat(),
        })
        return results

    def notify_relationship(self, event: RelationshipChangeEvent) -> dict[str, bool]:
        results = {}
        for ch in self._channels:
            if isinstance(ch, TelegramNotifier):
                results["telegram"] = ch.send_relationship_alert(event)
            elif isinstance(ch, SlackNotifier):
                results["slack"] = ch.send_relationship_alert(event)
        self._history.append({
            "type": "relationship",
            "results": results,
            "timestamp": datetime.now().isoformat(),
        })
        return results

    @classmethod
    def from_env(cls) -> "NotificationManager":
        mgr = cls()
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        slack_wh = os.environ.get("SLACK_WEBHOOK_URL", "")
        if tg_token and tg_chat:
            mgr.add_telegram(tg_token, tg_chat)
        if slack_wh:
            mgr.add_slack(slack_wh)
        return mgr

    @property
    def history(self) -> list[dict]:
        return list(self._history)

    @property
    def channel_count(self) -> int:
        return len(self._channels)
