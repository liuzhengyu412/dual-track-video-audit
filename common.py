"""审核项目共享的类型、运行状态与通用工具。"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


APP_VERSION = "0.4.0"

PASS = "通过"
RISK = "风险"
MANUAL = "人工复核"
SKIP = "暂不审核"
ALLOWED_STATUSES = {PASS, RISK, MANUAL, SKIP}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    name: str
    method: str


class AuditError(RuntimeError):
    """可直接展示给使用者的审核流程错误。"""


# 这些容器保持同一个对象引用，配置重载时只更新内容。
# 结果、报表和流程模块因此可以共享状态，而无需互相循环导入。
RULES: list[Rule] = []
RULE_BY_ID: dict[str, Rule] = {}
VISUAL_RULE_IDS: list[str] = []
AUDIO_RULE_IDS: list[str] = []
SYSTEM_SKIP_REASONS: dict[str, str] = {}
SCENARIOS: dict[str, dict[str, object]] = {}
ACTIVE_AUDIT_CONFIG: dict[str, object] = {}


CHINESE_DIGIT_MAP = {
    "零": "0",
    "〇": "0",
    "一": "1",
    "幺": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ[key] = value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_spoken_digits(value: str) -> str:
    digits: list[str] = []
    for char in value:
        if char.isdigit():
            digits.append(char)
        elif char in CHINESE_DIGIT_MAP:
            digits.append(CHINESE_DIGIT_MAP[char])
    return "".join(digits)


def safe_error_text(text: str) -> str:
    """保留审核信息，只遮盖可能出现在异常信息中的 API 密钥。"""
    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", text).replace("\n", " ")[:1200]


def ms_to_timestamp(milliseconds: int | float) -> str:
    total_seconds = max(0, int(round(float(milliseconds) / 1000)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def bypass_service_proxy(api_base: str) -> bool:
    enabled = os.getenv("MODEL_BYPASS_PROXY", os.getenv("DASHSCOPE_BYPASS_PROXY", "true"))
    if enabled.strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    hostname = urlparse(api_base).hostname
    if not hostname:
        return False
    bypass_hosts = [hostname]
    if hostname.endswith(".aliyuncs.com"):
        bypass_hosts.append(".aliyuncs.com")
    for key in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in os.getenv(key, "").split(",") if item.strip()]
        for bypass_host in bypass_hosts:
            if bypass_host not in entries:
                entries.append(bypass_host)
        os.environ[key] = ",".join(entries)
    return True


# 兼容原入口名称；具体服务适配器可逐步迁移到独立 provider。
bypass_broken_proxy_for_dashscope = bypass_service_proxy
