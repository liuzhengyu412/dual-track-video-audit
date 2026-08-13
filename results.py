"""审核结果校验、规范化与双轨合并。"""

from __future__ import annotations

import re
from typing import Any

from common import (
    ACTIVE_AUDIT_CONFIG,
    AUDIO_RULE_IDS,
    MANUAL,
    PASS,
    RISK,
    RULES,
    RULE_BY_ID,
    SKIP,
    SYSTEM_SKIP_REASONS,
    Rule,
    ms_to_timestamp,
    safe_error_text,
)


def preserve_review_text(value: Any, limit: int) -> str:
    """审核报告保留真实内容，仅限制单字段长度以避免异常大输出。"""
    return str(value)[:limit]


def local_validator_config(name: str) -> dict[str, Any] | None:
    validators = ACTIVE_AUDIT_CONFIG.get("local_validators", {})
    if not isinstance(validators, dict):
        return None
    value = validators.get(name)
    return value if isinstance(value, dict) and value.get("enabled", False) else None

def timestamp_seconds(value: Any) -> float | None:
    """把视频播放进度MM:SS或HH:MM:SS(.sss)转换为秒；格式无效时返回None。"""
    raw = str(value).strip()
    clock_match = re.fullmatch(
        r"(\d{1,3}):(\d{2}):(\d{2}(?:\.\d{1,3})?)", raw
    )
    if clock_match:
        hours, minutes, seconds = clock_match.groups()
        minute_value = int(minutes)
        second_value = float(seconds)
        if minute_value >= 60 or second_value >= 60:
            return None
        return int(hours) * 3600 + minute_value * 60 + second_value

    minute_match = re.fullmatch(r"(\d{1,3}):(\d{2}(?:\.\d{1,3})?)", raw)
    if not minute_match:
        return None
    minutes, seconds = minute_match.groups()
    second_value = float(seconds)
    if second_value >= 60:
        return None
    return int(minutes) * 60 + second_value


def normalize_timestamp_for_duration(
    value: Any,
    duration_seconds: float,
) -> tuple[str, bool]:
    """将MM:SS统一为HH:MM:SS，并修复把MM:SS误放进HH:MM的三段式错位。"""
    raw = str(value).strip()
    minute_match = re.fullmatch(r"(\d{1,3}):(\d{2}(?:\.\d{1,3})?)", raw)
    if minute_match:
        parsed = timestamp_seconds(raw)
        if parsed is not None and parsed <= duration_seconds + 2:
            return ms_to_timestamp(parsed * 1000), True
        return raw, False

    parsed = timestamp_seconds(raw)
    if parsed is not None and parsed <= duration_seconds + 2:
        return raw, False
    match = re.fullmatch(r"(\d{1,3}):(\d{2}):(00(?:\.0{1,3})?)", raw)
    if not match:
        return raw, False
    minutes, seconds, _ = match.groups()
    repaired_seconds = int(minutes) * 60 + int(seconds)
    if repaired_seconds > duration_seconds + 2:
        return raw, False
    return ms_to_timestamp(repaired_seconds * 1000), True


def normalize_timestamps_in_text(text: str, duration_seconds: float) -> str:
    def replace(match: re.Match[str]) -> str:
        repaired, changed = normalize_timestamp_for_duration(
            match.group(0), duration_seconds
        )
        return repaired if changed else match.group(0)

    timestamp_pattern = (
        r"(?<![\d:])(?:"
        r"\d{1,3}:\d{2}:\d{2}(?:\.\d{1,3})?"
        r"|\d{1,3}:\d{2}(?:\.\d{1,3})?"
        r")(?![:\d])"
    )
    return re.sub(timestamp_pattern, replace, text)


def repair_rule_payload_timestamps(
    model_output: dict[str, Any],
    *,
    duration_seconds: float,
) -> int:
    """就地修复规则证据中的可判定错位时间，并同步修正原因文本。"""
    repaired_count = 0
    raw_results = model_output.get("rule_results")
    if not isinstance(raw_results, list):
        return repaired_count
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason", ""))
        repaired_reason = normalize_timestamps_in_text(reason, duration_seconds)
        if repaired_reason != reason:
            item["reason"] = repaired_reason
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            continue
        for evidence_item in evidence:
            if not isinstance(evidence_item, dict):
                continue
            for key in ("start", "end"):
                repaired, changed = normalize_timestamp_for_duration(
                    evidence_item.get(key, ""), duration_seconds
                )
                if changed:
                    evidence_item[key] = repaired
                    repaired_count += 1
    return repaired_count


def validate_rule_payload(
    model_output: dict[str, Any],
    *,
    expected_rule_ids: tuple[str, ...],
    duration_seconds: float,
) -> list[str]:
    """检查模型JSON是否完整、置信度有效且证据时间位于视频范围内。"""
    errors: list[str] = []
    raw_results = model_output.get("rule_results")
    if not isinstance(raw_results, list):
        return ["rule_results不是数组"]

    returned_ids = [
        str(item.get("rule_id", "")).strip()
        for item in raw_results
        if isinstance(item, dict)
    ]
    if sorted(returned_ids) != sorted(expected_rule_ids):
        errors.append("rule_results必须且只能包含规定rule_id，每项恰好一次")

    for item in raw_results:
        if not isinstance(item, dict):
            errors.append("rule_results中存在非对象元素")
            continue
        rule_id = str(item.get("rule_id", "")).strip() or "未知规则"
        status = str(item.get("status", "")).strip()
        if status not in {PASS, RISK, MANUAL}:
            errors.append(f"{rule_id}的status无效")
        try:
            confidence = float(item["confidence"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{rule_id}缺少有效confidence")
            confidence = -1.0
        if not 0.0 <= confidence <= 1.0:
            errors.append(f"{rule_id}的confidence必须在0到1之间")
        elif status in {PASS, RISK} and confidence == 0.0:
            errors.append(f"{rule_id}已有明确结论但confidence为0")

        evidence = item.get("evidence", [])
        if not isinstance(evidence, list):
            errors.append(f"{rule_id}的evidence不是数组")
            continue
        for evidence_item in evidence:
            if not isinstance(evidence_item, dict):
                errors.append(f"{rule_id}包含无效证据对象")
                continue
            start = timestamp_seconds(evidence_item.get("start", ""))
            end = timestamp_seconds(evidence_item.get("end", ""))
            if start is None or end is None or end < start:
                errors.append(f"{rule_id}包含无效证据时间")
            elif start > duration_seconds + 2 or end > duration_seconds + 2:
                errors.append(f"{rule_id}证据时间超出视频总时长")
    return errors


def correction_prompt(
    base_prompt: str,
    *,
    errors: list[str],
    duration_seconds: float,
) -> str:
    return (
        base_prompt
        + "\n\n上一次输出校验失败，请重新输出完整JSON。必须修正以下问题：\n- "
        + "\n- ".join(errors[:20])
        + f"\n视频总时长为{duration_seconds:.3f}秒，所有证据时间必须是从00:00:00开始的播放进度且不得超过总时长。"
        + "\n不得沿用画面中的系统时钟；通过或风险结论必须给出大于0的真实confidence。"
    )


def sanitize_evidence(value: Any, source: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, str]] = []
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        detail = preserve_review_text(item.get("detail", ""), 500)
        if source:
            detail = f"[{source}] {detail}"
        sanitized.append(
            {
                "start": str(item.get("start", ""))[:20],
                "end": str(item.get("end", ""))[:20],
                "detail": detail,
            }
        )
    return sanitized


def normalize_rule_results(
    model_output: dict[str, Any],
    *,
    expected_rule_ids: tuple[str, ...],
    source: str,
    confidence_threshold: float,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    if duration_seconds is not None:
        repair_rule_payload_timestamps(
            model_output, duration_seconds=duration_seconds
        )
    raw_results = model_output.get("rule_results")
    if not isinstance(raw_results, list):
        raw_results = []
    normalized_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        rule_id = str(item.get("rule_id", "")).strip()
        if rule_id not in expected_rule_ids or rule_id in normalized_by_id:
            continue
        status = str(item.get("status", "")).strip()
        if status not in {PASS, RISK, MANUAL}:
            status = MANUAL
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = preserve_review_text(str(item.get("reason", "")).strip(), 1600)
        if not reason:
            reason = f"{source}模型未提供原因。"
        evidence = sanitize_evidence(item.get("evidence", []), source)
        evidence_time_invalid = False
        if duration_seconds is not None:
            for evidence_item in evidence:
                start = timestamp_seconds(evidence_item.get("start", ""))
                end = timestamp_seconds(evidence_item.get("end", ""))
                if (
                    start is None
                    or end is None
                    or end < start
                    or start > duration_seconds + 2
                    or end > duration_seconds + 2
                ):
                    evidence_time_invalid = True
                    break
        if evidence_time_invalid:
            status = MANUAL
            confidence = 0.0
            reason = f"证据时间无效或超出视频总时长；{reason}"
            evidence = []
        elif status in {PASS, RISK} and confidence == 0.0:
            status = MANUAL
            reason = f"模型返回明确结论但置信度为0；{reason}"
        elif status == PASS and confidence < confidence_threshold:
            status = MANUAL
            reason = (
                f"通过置信度{confidence:.2f}低于阈值{confidence_threshold:.2f}；{reason}"
            )
        rule = RULE_BY_ID[rule_id]
        normalized_by_id[rule_id] = {
            "rule_id": rule_id,
            "category": rule.category,
            "name": rule.name,
            "method": rule.method,
            "source": source,
            "status": status,
            "confidence": confidence,
            "reason": reason,
            "evidence": evidence,
        }

    for rule_id in expected_rule_ids:
        if rule_id in normalized_by_id:
            continue
        rule = RULE_BY_ID[rule_id]
        normalized_by_id[rule_id] = {
            "rule_id": rule_id,
            "category": rule.category,
            "name": rule.name,
            "method": rule.method,
            "source": source,
            "status": MANUAL,
            "confidence": 0.0,
            "reason": f"{source}模型未返回该审核项，自动转人工复核。",
            "evidence": [],
        }
    return {"rules": [normalized_by_id[rule_id] for rule_id in expected_rule_ids]}


def make_failure_source(
    expected_rule_ids: tuple[str, ...],
    *,
    source: str,
    reason: str,
) -> dict[str, Any]:
    safe_reason = safe_error_text(reason)
    rules: list[dict[str, Any]] = []
    for rule_id in expected_rule_ids:
        rule = RULE_BY_ID[rule_id]
        rules.append(
            {
                "rule_id": rule_id,
                "category": rule.category,
                "name": rule.name,
                "method": rule.method,
                "source": source,
                "status": MANUAL,
                "confidence": 0.0,
                "reason": f"{source}审核失败，转人工复核：{safe_reason}",
                "evidence": [],
            }
        )
    return {"rules": rules, "error": safe_reason}


def normalize_script_checks(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    allowed = {"matched", "risk", "unreviewed"}
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "unreviewed")).lower()
        if status not in allowed:
            status = "unreviewed"
        result.append(
            {
                "item": preserve_review_text(item.get("item", ""), 1000),
                "status": status,
                "heard_text": preserve_review_text(item.get("heard_text", ""), 1000),
                "start": str(item.get("start", ""))[:20],
                "end": str(item.get("end", ""))[:20],
                "reason": preserve_review_text(item.get("reason", ""), 500),
            }
        )
    return result


def build_dialogue_from_asr(
    sentences: list[dict[str, Any]],
    speaker_labels: Any,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """以ASR原始句段为唯一文本源，模型只能按segment_id追加说话人标签。"""
    source_items: list[dict[str, str]] = []
    for index, sentence in enumerate(sentences, start=1):
        text = preserve_review_text(str(sentence.get("text", "")).strip(), 2000)
        if not text:
            continue
        segment_id = str(sentence.get("segment_id", "")).strip() or f"SEG-{index:03d}"
        source_items.append(
            {
                "segment_id": segment_id,
                "start": str(sentence.get("start", ""))[:20],
                "end": str(sentence.get("end", ""))[:20],
                "text": text,
            }
        )

    expected_ids = [item["segment_id"] for item in source_items]
    expected_set = set(expected_ids)
    labels_by_id: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    invalid_ids: set[str] = set()
    if isinstance(speaker_labels, list):
        for item in speaker_labels:
            if not isinstance(item, dict):
                continue
            segment_id = str(item.get("segment_id", "")).strip()
            if segment_id not in expected_set:
                if segment_id:
                    invalid_ids.add(segment_id)
                continue
            if segment_id in labels_by_id:
                duplicate_ids.add(segment_id)
                continue
            speaker = str(item.get("speaker", "unknown")).strip().lower()
            allowed_roles = set(ACTIVE_AUDIT_CONFIG.get("speaker_roles", []))
            if speaker not in allowed_roles:
                speaker = "unknown"
            labels_by_id[segment_id] = speaker

    missing_ids = [segment_id for segment_id in expected_ids if segment_id not in labels_by_id]
    dialogue = [
        {
            "segment_id": item["segment_id"],
            "speaker": labels_by_id.get(item["segment_id"], "unknown"),
            "start": item["start"],
            "end": item["end"],
            "text": item["text"],
        }
        for item in source_items
    ]
    source_count = len(source_items)
    labelled_count = source_count - len(missing_ids)
    integrity = {
        "asr_segment_count": source_count,
        "dialogue_segment_count": len(dialogue),
        "text_coverage": 1.0 if source_count else 0.0,
        "speaker_label_coverage": (
            round(labelled_count / source_count, 6) if source_count else 0.0
        ),
        "missing_segment_ids": missing_ids,
        "duplicate_segment_ids": sorted(duplicate_ids),
        "invalid_segment_ids": sorted(invalid_ids),
        "unknown_speaker_count": sum(
            1 for item in dialogue if item["speaker"] == "unknown"
        ),
    }
    return dialogue, integrity


def validate_speaker_labels(
    model_output: dict[str, Any],
    sentences: list[dict[str, Any]],
) -> list[str]:
    """检查模型是否对每个ASR片段恰好返回一个说话人标签。"""
    _, integrity = build_dialogue_from_asr(
        sentences, model_output.get("speaker_labels", [])
    )
    errors: list[str] = []
    if integrity["missing_segment_ids"]:
        errors.append(
            "speaker_labels缺少segment_id："
            + ",".join(integrity["missing_segment_ids"][:20])
        )
    if integrity["duplicate_segment_ids"]:
        errors.append(
            "speaker_labels存在重复segment_id："
            + ",".join(integrity["duplicate_segment_ids"][:20])
        )
    if integrity["invalid_segment_ids"]:
        errors.append(
            "speaker_labels包含不存在的segment_id："
            + ",".join(integrity["invalid_segment_ids"][:20])
        )
    return errors


def sanitize_asr_sentences(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, sentence in enumerate(sentences, start=1):
        result.append(
            {
                "segment_id": str(sentence.get("segment_id", "")).strip()
                or f"SEG-{index:03d}",
                "start": sentence.get("start", ""),
                "end": sentence.get("end", ""),
                "text": preserve_review_text(sentence.get("text", ""), 2000),
            }
        )
    return result


def rules_by_id(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("rule_id")): item
        for item in source.get("rules", [])
        if isinstance(item, dict) and item.get("rule_id")
    }


def combine_source_items(
    rule: Rule,
    source_items: list[dict[str, Any]],
) -> dict[str, Any]:
    if not source_items:
        return {
            "rule_id": rule.rule_id,
            "category": rule.category,
            "name": rule.name,
            "method": rule.method,
            "status": MANUAL,
            "confidence": 0.0,
            "reason": "没有可用的模型审核来源，自动转人工复核。",
            "evidence": [],
            "source_statuses": {},
        }

    statuses = [str(item.get("status", MANUAL)) for item in source_items]
    if RISK in statuses:
        status = RISK
        relevant = [item for item in source_items if item.get("status") == RISK]
    elif MANUAL in statuses:
        status = MANUAL
        relevant = [item for item in source_items if item.get("status") == MANUAL]
    elif statuses and all(item == PASS for item in statuses):
        status = PASS
        relevant = source_items
    else:
        status = MANUAL
        relevant = source_items

    confidence = max((float(item.get("confidence", 0.0)) for item in relevant), default=0.0)
    reasons = []
    evidence: list[dict[str, str]] = []
    source_statuses: dict[str, str] = {}
    for item in source_items:
        source = str(item.get("source", "模型"))
        source_statuses[source] = str(item.get("status", MANUAL))
        reasons.append(f"{source}：{item.get('reason', '')}")
        evidence.extend(item.get("evidence", []))
    return {
        "rule_id": rule.rule_id,
        "category": rule.category,
        "name": rule.name,
        "method": rule.method,
        "status": status,
        "confidence": min(1.0, max(0.0, confidence)),
        "reason": preserve_review_text("；".join(reasons), 1600),
        "evidence": evidence[:12],
        "source_statuses": source_statuses,
    }


def set_rule_status(
    aggregate_by_id: dict[str, dict[str, Any]],
    rule_id: str,
    *,
    status: str,
    reason: str,
    confidence: float,
    evidence: list[dict[str, str]] | None = None,
) -> None:
    item = aggregate_by_id[rule_id]
    current = item.get("status")
    priority = {SKIP: 0, PASS: 1, MANUAL: 2, RISK: 3}
    if priority.get(status, 0) < priority.get(str(current), 0):
        return
    item["status"] = status
    item["confidence"] = min(1.0, max(float(item.get("confidence", 0.0)), confidence))
    item["reason"] = preserve_review_text(reason, 1600)
    if evidence:
        item["evidence"] = (item.get("evidence", []) + evidence)[:12]


def apply_script_check_rules(
    aggregate_by_id: dict[str, dict[str, Any]],
    script_checks: list[dict[str, str]],
    rule_id: str,
) -> None:
    if rule_id not in aggregate_by_id:
        return
    if not script_checks:
        if aggregate_by_id[rule_id]["status"] == PASS:
            set_rule_status(
                aggregate_by_id,
                rule_id,
                status=MANUAL,
                confidence=0.0,
                reason="模型未返回逐项话术比对，不能直接判定话术完整通过。",
            )
        return
    risks = [item for item in script_checks if item.get("status") == "risk"]
    if risks:
        evidence = [
            {
                "start": item.get("start", ""),
                "end": item.get("end", ""),
                "detail": f"[话术比对] {item.get('reason', '')}",
            }
            for item in risks[:8]
        ]
        set_rule_status(
            aggregate_by_id,
            rule_id,
            status=RISK,
            confidence=0.95,
            reason="逐项话术比对发现漏问、关键内容缺失或无法满足标准话术要求。",
            evidence=evidence,
        )
        return
    if any(item.get("status") == "unreviewed" for item in script_checks):
        if aggregate_by_id[rule_id]["status"] == PASS:
            set_rule_status(
                aggregate_by_id,
                rule_id,
                status=MANUAL,
                confidence=0.0,
                reason="部分标准话术事项未完成核验，转人工复核。",
            )


def apply_audio_metric_rules(
    aggregate_by_id: dict[str, dict[str, Any]],
    audio_metrics: dict[str, Any],
    rule_ids: list[str],
) -> None:
    applicable_ids = [rule_id for rule_id in rule_ids if rule_id in aggregate_by_id]
    if not applicable_ids:
        return
    rms = float(audio_metrics.get("rms_normalized", 0.0) or 0.0)
    silence_ratio = float(audio_metrics.get("silence_ratio", 1.0) or 1.0)
    clipping_ratio = float(audio_metrics.get("clipping_ratio", 0.0) or 0.0)
    if rms < 0.0005 or silence_ratio > 0.99:
        for rule_id in applicable_ids:
            set_rule_status(
                aggregate_by_id,
                rule_id,
                status=RISK,
                confidence=0.98,
                reason="本地音频指标显示音轨接近无声，无法支持合规问答核验。",
            )
    elif clipping_ratio > 0.02:
        for rule_id in applicable_ids:
            if aggregate_by_id[rule_id]["status"] == PASS:
                set_rule_status(
                    aggregate_by_id,
                    rule_id,
                    status=MANUAL,
                    confidence=0.0,
                    reason="本地音频指标显示明显削波失真，需人工复核语音质量。",
                )


def combine_audit_results(
    *,
    visual_source: dict[str, Any],
    audio_source: dict[str, Any],
    script_checks: list[dict[str, str]],
    audio_metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    visual_by_id = rules_by_id(visual_source)
    audio_by_id = rules_by_id(audio_source)
    aggregate: list[dict[str, Any]] = []
    for rule in RULES:
        if rule.rule_id in SYSTEM_SKIP_REASONS:
            aggregate.append(
                {
                    "rule_id": rule.rule_id,
                    "category": rule.category,
                    "name": rule.name,
                    "method": rule.method,
                    "status": SKIP,
                    "confidence": 1.0,
                    "reason": SYSTEM_SKIP_REASONS[rule.rule_id],
                    "evidence": [],
                    "source_statuses": {"系统数据": SKIP},
                }
            )
            continue
        source_items: list[dict[str, Any]] = []
        if rule.rule_id in visual_by_id:
            source_items.append(visual_by_id[rule.rule_id])
        if rule.rule_id in audio_by_id:
            source_items.append(audio_by_id[rule.rule_id])
        aggregate.append(combine_source_items(rule, source_items))

    aggregate_by_id = {item["rule_id"]: item for item in aggregate}
    script_validator = local_validator_config("script_coverage")
    script_settings = ACTIVE_AUDIT_CONFIG.get("script_comparison", {})
    if (
        script_validator
        and isinstance(script_settings, dict)
        and script_settings.get("enabled")
    ):
        apply_script_check_rules(
            aggregate_by_id, script_checks, str(script_validator.get("rule_id", ""))
        )
    metrics_validator = local_validator_config("audio_metrics")
    if metrics_validator:
        apply_audio_metric_rules(
            aggregate_by_id, audio_metrics,
            [str(value) for value in metrics_validator.get("rule_ids", [])],
        )
    return [aggregate_by_id[rule.rule_id] for rule in RULES]


def overall_status(aggregate: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status")) for item in aggregate}
    if RISK in statuses:
        return RISK
    if MANUAL in statuses:
        return MANUAL
    if SKIP in statuses:
        return "部分审核通过"
    return PASS


def routing_review_items(routing: Any) -> list[dict[str, Any]]:
    """把无法自动审核的分流内容变成显式待复核项。"""
    if not isinstance(routing, dict):
        return []
    items: list[dict[str, Any]] = []
    manual_text = str(routing.get("manual_or_system_requirements", "")).strip()
    if manual_text:
        items.append(
            {
                "rule_id": "routing_manual_or_system",
                "category": "人工/系统要求",
                "name": "需要人工或外部系统确认的审核要求",
                "method": "人工或外部系统",
                "status": MANUAL,
                "confidence": 0.0,
                "reason": preserve_review_text(manual_text, 4000),
                "evidence": [],
                "source_statuses": {"规则自动分流": MANUAL},
            }
        )
    uncertain = routing.get("uncertain_items", [])
    if isinstance(uncertain, list):
        for index, value in enumerate(uncertain, start=1):
            text = str(value).strip()
            if not text:
                continue
            items.append(
                {
                    "rule_id": f"routing_uncertain_{index}",
                    "category": "待确认分流",
                    "name": f"无法可靠分类的审核要求 {index}",
                    "method": "人工确认",
                    "status": MANUAL,
                    "confidence": 0.0,
                    "reason": preserve_review_text(text, 4000),
                    "evidence": [],
                    "source_statuses": {"规则自动分流": MANUAL},
                }
            )
    return items
