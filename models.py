"""模型服务调用与 ASR 转写适配。"""

from __future__ import annotations

import base64
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from common import (
    AuditError,
    bypass_broken_proxy_for_dashscope,
    ms_to_timestamp,
    safe_error_text,
)
from media import split_wav

def response_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    for method_name in ("to_dict", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, dict):
                return result
    try:
        result = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuditError(f"无法解析接口返回数据：{type(value).__name__}") from exc
    if not isinstance(result, dict):
        raise AuditError("接口返回数据顶层不是对象。")
    return result


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise AuditError("Qwen3-VL没有返回JSON对象。")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AuditError(f"Qwen3-VL返回的JSON无法解析：{exc}") from exc
    if not isinstance(value, dict):
        raise AuditError("Qwen3-VL返回的JSON顶层必须是对象。")
    return value


def extract_qwen_text(response_dict: dict[str, Any]) -> str:
    choices = response_dict.get("output", {}).get("choices", [])
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def call_qwen(
    *,
    prompt: str,
    api_key: str,
    model: str,
    api_base: str,
    video_path: Path | None = None,
    fps: float = 2.0,
) -> dict[str, Any]:
    bypass_broken_proxy_for_dashscope(api_base)
    try:
        import dashscope
        from dashscope import MultiModalConversation
    except ImportError as exc:
        raise AuditError("缺少 dashscope，请先安装 requirements.txt。") from exc

    dashscope.api_key = api_key
    dashscope.base_http_api_url = api_base
    content: list[dict[str, Any]] = []
    if video_path is not None:
        content.append({"video": video_path.resolve().as_uri(), "fps": fps})
    content.append({"text": prompt})
    messages = [{"role": "user", "content": content}]

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            responses = MultiModalConversation.call(
                api_key=api_key,
                model=model,
                messages=messages,
                result_format="message",
                response_format={"type": "json_object"},
                stream=True,
                incremental_output=True,
                temperature=0,
                seed=20250826,
            )
            text_parts: list[str] = []
            for response in responses:
                response_dict = response_to_dict(response)
                status_code = response_dict.get(
                    "status_code", getattr(response, "status_code", 200)
                )
                if status_code and int(status_code) != 200:
                    message = (
                        response_dict.get("message")
                        or response_dict.get("code")
                        or response_dict
                    )
                    raise AuditError(
                        f"Qwen3-VL调用失败：{safe_error_text(str(message))}"
                    )
                chunk_text = extract_qwen_text(response_dict)
                if not chunk_text:
                    output_obj = getattr(response, "output", None)
                    choices = getattr(output_obj, "choices", []) if output_obj else []
                    if choices:
                        content_obj = choices[0].message.content
                        if isinstance(content_obj, list):
                            chunk_text = "".join(
                                str(block.get("text", ""))
                                for block in content_obj
                                if isinstance(block, dict) and block.get("text")
                            )
                if chunk_text:
                    text_parts.append(chunk_text)
            text = "".join(text_parts)
            if not text:
                raise AuditError("Qwen3-VL返回内容为空。")
            return parse_json_object(text)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(2)
    raise AuditError(f"Qwen3-VL调用失败：{safe_error_text(str(last_error))}")


def audio_to_data_uri(audio_path: Path) -> str:
    encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    return f"data:audio/wav;base64,{encoded}"


def decumulate_asr_sentences(
    sentences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把Fun-ASR的累积快照转换为只包含新增内容的连续片段。"""
    ordered = sorted(
        (dict(item) for item in sentences if isinstance(item, dict)),
        key=lambda item: (
            int(item.get("end_ms", 0) or 0),
            int(item.get("begin_ms", 0) or 0),
        ),
    )
    result: list[dict[str, Any]] = []
    previous_snapshot = ""
    previous_snapshot_begin = 0
    previous_snapshot_end = 0
    for item in ordered:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        begin_ms = int(item.get("begin_ms", 0) or 0)
        end_ms = int(item.get("end_ms", begin_ms) or begin_ms)
        incremental_text = text
        incremental_begin = begin_ms

        same_origin = bool(previous_snapshot) and abs(begin_ms - previous_snapshot_begin) <= 200
        if same_origin and text.startswith(previous_snapshot):
            incremental_text = text[len(previous_snapshot) :].strip()
            incremental_begin = max(begin_ms, previous_snapshot_end)
        elif same_origin and previous_snapshot.startswith(text):
            incremental_text = ""

        previous_snapshot = text
        previous_snapshot_begin = begin_ms
        previous_snapshot_end = max(previous_snapshot_end, end_ms)
        if not incremental_text:
            continue

        words = item.get("words", [])
        if isinstance(words, list) and incremental_begin > begin_ms:
            words = [
                word
                for word in words
                if isinstance(word, dict)
                and int(word.get("end_time", 0) or 0) > incremental_begin
            ]
        result.append(
            {
                **item,
                "begin_ms": incremental_begin,
                "end_ms": max(incremental_begin, end_ms),
                "text": incremental_text,
                "words": words if isinstance(words, list) else [],
            }
        )
    return result


def call_fun_asr_chunk(
    *,
    chunk_path: Path,
    api_key: str,
    model: str,
    api_base: str,
) -> dict[str, Any]:
    try:
        import requests
    except ImportError as exc:
        raise AuditError("缺少 requests，请先安装 requirements.txt。") from exc

    bypass_proxy = bypass_broken_proxy_for_dashscope(api_base)
    endpoint = f"{api_base}/services/aigc/multimodal-generation/generation"
    payload = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"audio": audio_to_data_uri(chunk_path)}],
                }
            ]
        },
        "parameters": {"format": "wav", "vad_enabled": True},
        "resources": [],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "enable",
    }

    try:
        session = requests.Session()
        session.trust_env = not bypass_proxy
        response = session.post(
            endpoint,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(30, 900),
        )
    except requests.RequestException as exc:
        raise AuditError(f"Fun-ASR网络请求失败：{safe_error_text(str(exc))}") from exc

    if response.status_code != 200:
        body = safe_error_text(response.text)
        raise AuditError(f"Fun-ASR调用失败：HTTP {response.status_code}，{body}")

    event_objects: list[dict[str, Any]] = []
    raw_lines: list[str] = []
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace").strip()
        raw_lines.append(line)
        if not line.startswith("data:"):
            continue
        data_text = line[5:].strip()
        if not data_text or data_text == "[DONE]":
            continue
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError as exc:
            raise AuditError(f"Fun-ASR返回了无法解析的SSE数据：{exc}") from exc
        if isinstance(event, dict):
            event_objects.append(event)

    if not event_objects:
        combined = "\n".join(raw_lines).strip()
        try:
            fallback = json.loads(combined)
        except json.JSONDecodeError as exc:
            raise AuditError("Fun-ASR未返回可识别的JSON或SSE结果。") from exc
        if isinstance(fallback, dict):
            event_objects.append(fallback)

    finalized_sentences: dict[int, dict[str, Any]] = {}
    cumulative_text = ""
    request_id = ""
    usage_duration = 0
    for event in event_objects:
        if event.get("code") or event.get("message") and not event.get("output"):
            raise AuditError(
                f"Fun-ASR返回错误：{safe_error_text(str(event.get('message') or event.get('code')))}"
            )
        request_id = str(event.get("request_id", request_id))
        output = event.get("output", {})
        if not isinstance(output, dict):
            continue
        cumulative_text = str(output.get("text", cumulative_text))
        sentence = output.get("sentence", {})
        if isinstance(sentence, dict) and sentence.get("sentence_end"):
            try:
                sentence_id = int(sentence.get("sentence_id", len(finalized_sentences) + 1))
            except (TypeError, ValueError):
                sentence_id = len(finalized_sentences) + 1
            finalized_sentences[sentence_id] = {
                "sentence_id": sentence_id,
                "begin_ms": int(sentence.get("begin_time", 0) or 0),
                "end_ms": int(sentence.get("end_time", sentence.get("begin_time", 0)) or 0),
                "text": str(sentence.get("text", "")).strip(),
                "words": sentence.get("words", []) if isinstance(sentence.get("words"), list) else [],
            }
        usage = event.get("usage", {})
        if isinstance(usage, dict):
            try:
                usage_duration = max(usage_duration, int(usage.get("duration", 0) or 0))
            except (TypeError, ValueError):
                pass

    sentences = decumulate_asr_sentences(
        [finalized_sentences[key] for key in sorted(finalized_sentences)]
    )
    if not sentences and cumulative_text.strip():
        sentences = [
            {
                "sentence_id": 1,
                "begin_ms": 0,
                "end_ms": usage_duration * 1000,
                "text": cumulative_text.strip(),
                "words": [],
            }
        ]
    if not sentences:
        raise AuditError("Fun-ASR返回成功，但没有得到有效转写文本。")
    return {
        "request_id": request_id,
        "usage_duration_seconds": usage_duration,
        "text": cumulative_text.strip(),
        "sentences": sentences,
    }


def transcribe_audio_chunks(
    *,
    chunks: list[dict[str, Any]],
    api_key: str,
    model: str,
    api_base: str,
) -> dict[str, Any]:
    all_sentences: list[dict[str, Any]] = []
    request_ids: list[str] = []
    pending = [dict(chunk, retry_depth=0) for chunk in chunks]
    active_model = model
    print(
        f"[音频] {active_model}正在转写完整音频并恢复连续时间轴……",
        flush=True,
    )
    while pending:
        chunk = pending.pop(0)
        try:
            response = call_fun_asr_chunk(
                chunk_path=chunk["path"],
                api_key=api_key,
                model=active_model,
                api_base=api_base,
            )
        except Exception as exc:
            error_text = str(exc).lower()
            if (
                "there are no suitable services" in error_text
                and active_model != "fun-asr-realtime"
            ):
                active_model = "fun-asr-realtime"
                pending.insert(0, chunk)
                print(
                    "[音频] 当前Fun-ASR快照模型在此API Key或地域不可用，"
                    "已自动回退到稳定版fun-asr-realtime。",
                    flush=True,
                )
                continue
            retryable = any(
                marker in error_text
                for marker in (
                    "timeout",
                    "timed out",
                    "connection aborted",
                    "connection reset",
                    "write operation timed out",
                )
            )
            duration = float(chunk.get("duration_seconds", 0.0) or 0.0)
            retry_depth = int(chunk.get("retry_depth", 0) or 0)
            if not retryable or duration <= 30.0 or retry_depth >= 4:
                raise

            retry_seconds = max(30, min(120, int(math.ceil(duration / 2))))
            retry_dir = (
                Path(chunk["path"]).parent
                / f"retry_{int(chunk.get('index', 0) or 0):03d}_{retry_depth + 1}"
            )
            smaller_chunks = split_wav(
                Path(chunk["path"]), retry_dir, retry_seconds, AuditError
            )
            base_offset_ms = int(chunk.get("offset_ms", 0) or 0)
            for smaller in smaller_chunks:
                smaller["offset_ms"] = base_offset_ms + int(
                    smaller.get("offset_ms", 0) or 0
                )
                smaller["retry_depth"] = retry_depth + 1
            pending[0:0] = smaller_chunks
            print(
                f"[音频] 当前传输块超时，已自动缩短为约{retry_seconds}秒后重试；"
                "最终仍按完整时间轴合并审核。",
                flush=True,
            )
            continue

        if response.get("request_id"):
            request_ids.append(str(response["request_id"]))
        offset_ms = int(chunk["offset_ms"])
        for sentence in response["sentences"]:
            begin_ms = int(sentence.get("begin_ms", 0)) + offset_ms
            end_ms = int(sentence.get("end_ms", 0)) + offset_ms
            text = str(sentence.get("text", "")).strip()
            if not text:
                continue
            words: list[dict[str, Any]] = []
            for word in sentence.get("words", []):
                if not isinstance(word, dict):
                    continue
                words.append(
                    {
                        "text": str(word.get("text", "")),
                        "begin_ms": int(word.get("begin_time", 0) or 0) + offset_ms,
                        "end_ms": int(word.get("end_time", 0) or 0) + offset_ms,
                        "punctuation": str(word.get("punctuation", "")),
                    }
                )
            all_sentences.append(
                {
                    "begin_ms": begin_ms,
                    "end_ms": end_ms,
                    "start": ms_to_timestamp(begin_ms),
                    "end": ms_to_timestamp(end_ms),
                    "text": text,
                    "words": words,
                }
            )

    all_sentences.sort(key=lambda item: (item["begin_ms"], item["end_ms"]))
    for index, sentence in enumerate(all_sentences, start=1):
        sentence["segment_id"] = f"SEG-{index:03d}"
    full_text = "".join(sentence["text"] for sentence in all_sentences)
    return {
        "model": active_model,
        "requested_model": model,
        "request_ids": request_ids,
        "sentences": all_sentences,
        "text": full_text,
        "chunk_count": len(chunks),
    }
