#!/usr/bin/env python3
"""配置驱动的视频音频/视觉双轨审核入口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
PIPELINE_PATH = PROJECT_DIR / "audit_pipeline.py"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "rules" / "audit_config.json"
ENV_PATH = PROJECT_DIR / ".env"
LOCAL_VENV_DIR = PROJECT_DIR / ".venv"
DEFAULT_TEXT_MODEL = "qwen3-32b"

class PreflightError(RuntimeError):
    """运行前审核依据或引擎检查失败。"""


def enter_local_venv() -> None:
    """存在本地.venv时自动使用其中的Python，省去手动激活。"""
    venv_python = LOCAL_VENV_DIR / "bin" / "python"
    if not venv_python.is_file():
        return
    if Path(sys.prefix).resolve() == LOCAL_VENV_DIR.resolve():
        return
    os.execv(
        str(venv_python),
        [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
    )


def load_engine():
    if not PIPELINE_PATH.is_file():
        raise PreflightError(f"找不到本地审核流程模块：{PIPELINE_PATH}")
    try:
        import audit_pipeline
    except Exception as exc:
        raise PreflightError(f"无法加载本地审核流程模块：{exc}") from exc
    return audit_pipeline


def validate_basis(engine, config_path: Path) -> dict[str, object]:
    try:
        config = engine.configure_audit_config(config_path)
    except engine.AuditError as exc:
        raise PreflightError(str(exc)) from exc
    configured_scripts = {
        scenario_id: str((config_path.parent / item["script"]).resolve())
        for scenario_id, item in config["scenarios"].items()
        if item["script"]
    }
    scripts = configured_scripts if engine.script_comparison_enabled() else {}
    missing = [path for path in scripts.values() if not Path(path).is_file()]
    if missing:
        raise PreflightError("找不到场景话术文件：" + "、".join(missing))
    tracked_files = [config_path, engine.DEFAULT_SETTINGS_PATH]
    review_path = config.get("review_brief_path")
    if review_path:
        tracked_files.append(Path(review_path))
    for relative_path in config.get("prompts", {}).values():
        prompt_path = (config_path.parent / str(relative_path)).resolve()
        if prompt_path.is_file():
            tracked_files.append(prompt_path)
    tracked_files.extend(Path(path) for path in scripts.values())
    basis_hashes = {
        str(path.resolve()): engine.sha256_file(path)
        for path in tracked_files
        if path.is_file()
    }
    return {
        "config_file": str(config_path.resolve()),
        "config_sha256": engine.sha256_file(config_path),
        "rule_count": str(len(config["rules"])),
        "scenarios": ", ".join(sorted(config["scenarios"])),
        "mode": "配置驱动：视觉轨与音频轨独立审核后由本地规则合并",
        "settings_file": str(engine.DEFAULT_SETTINGS_PATH.resolve()),
        "settings_sha256": engine.sha256_file(engine.DEFAULT_SETTINGS_PATH),
        "basis_file_hashes": basis_hashes,
    }


def call_text_generation(
    engine,
    *,
    prompt: str,
    api_key: str,
    model: str,
    api_base: str,
) -> dict:
    """通过百炼文本生成接口调用开源Qwen文本模型，并返回JSON对象。"""
    engine.bypass_broken_proxy_for_dashscope(api_base)
    try:
        import dashscope
        from dashscope import Generation
    except ImportError as exc:
        raise engine.AuditError("缺少dashscope，请先安装requirements.txt。") from exc

    dashscope.api_key = api_key
    dashscope.base_http_api_url = api_base
    try:
        responses = Generation.call(
            api_key=api_key,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            result_format="message",
            response_format={"type": "json_object"},
            enable_thinking=False,
            stream=True,
            incremental_output=True,
            temperature=0,
            seed=20250826,
        )
        text_parts = []
        for response in responses:
            response_dict = engine.response_to_dict(response)
            status_code = response_dict.get(
                "status_code", getattr(response, "status_code", 200)
            )
            if status_code and int(status_code) != 200:
                message = (
                    response_dict.get("message")
                    or response_dict.get("code")
                    or response_dict
                )
                raise engine.AuditError(
                    "文本模型调用失败：" + engine.safe_error_text(str(message))
                )
            chunk_text = engine.extract_qwen_text(response_dict)
            if chunk_text:
                text_parts.append(chunk_text)
        text = "".join(text_parts)
        if not text:
            raise engine.AuditError("文本模型返回内容为空。")
        return engine.parse_json_object(text)
    except engine.AuditError:
        raise
    except Exception as exc:
        raise engine.AuditError(
            "文本模型调用失败：" + engine.safe_error_text(str(exc))
        ) from exc


def configure_engine(
    engine,
    basis: dict[str, object],
    credentials: dict[str, str] | None = None,
    text_model: str = DEFAULT_TEXT_MODEL,
) -> None:
    engine.PROJECT_DIR = PROJECT_DIR
    engine.WORK_DIR = PROJECT_DIR / "work"
    engine.DEFAULT_OUTPUT_DIR = PROJECT_DIR / "results"
    engine.AUDIO_ANALYSIS_MODEL = text_model

    original_build_parser = engine.build_parser
    original_audit_video = engine.audit_video

    def build_parser_with_dual_track_help():
        parser = original_build_parser()
        processing = engine.ACTIVE_AUDIT_CONFIG.get("processing", {})
        parser.set_defaults(
            fps=processing.get("fps", 2.0),
            audio_chunk_seconds=processing.get("audio_chunk_seconds", 180),
            confidence_threshold=processing.get("confidence_threshold", 0.70),
            keep_work_files=processing.get("keep_work_files", False),
        )
        for action in parser._actions:
            if action.dest == "visual_model":
                action.help = "视觉轨模型；默认读取.env中的VISION_MODEL"
            elif action.dest == "asr_model":
                action.help = "ASR轨模型；默认读取.env中的ASR_MODEL"
            elif action.dest == "fps":
                action.help = "视觉抽帧率；2.0表示每秒2帧，不是总共2帧"
            elif action.dest == "output_dir":
                action.help = "结果目录，默认当前目录下的results"
        parser.add_argument(
            "--text-model",
            default=text_model,
            help="文本分析模型；默认读取.env中的TEXT_MODEL",
        )
        parser.add_argument(
            "--settings",
            type=Path,
            default=engine.DEFAULT_SETTINGS_PATH,
            help="项目设置 JSON；可与 --config 组成一套独立审核方案",
        )
        return parser

    engine.build_parser = build_parser_with_dual_track_help

    if credentials:
        original_call_qwen = engine.call_qwen
        original_transcribe = engine.transcribe_audio_chunks

        if engine.ACTIVE_AUDIT_CONFIG.get("mode") == "simple":
            routing_mode = engine.ACTIVE_AUDIT_CONFIG.get("rule_routing", "ai")
            if routing_mode != "ai":
                raise PreflightError("简易模式目前只支持 settings.json 中 rule_routing 为 ai。")
            try:
                routing = call_text_generation(
                    engine,
                    prompt=engine.build_rule_routing_prompt(),
                    api_key=credentials["text"],
                    model=text_model,
                    api_base=os.getenv(
                        "MODEL_API_BASE",
                        os.getenv("DASHSCOPE_BASE_HTTP_API_URL", engine.DEFAULT_API_BASE),
                    ),
                )
            except engine.AuditError as exc:
                strategy = engine.ACTIVE_AUDIT_CONFIG.get(
                    "routing_failure_strategy", "fallback_both"
                )
                if strategy == "stop":
                    raise PreflightError(f"审核规则自动分流失败：{exc}") from exc
                full_rules = str(
                    engine.ACTIVE_AUDIT_CONFIG.get("global_instruction", "")
                ).strip()
                routing = {
                    "visual_requirements": full_rules,
                    "audio_requirements": full_rules,
                    "combined_requirements": "",
                    "manual_or_system_requirements": "规则自动分流失败，请人工确认分流准确性。",
                    "uncertain_items": [engine.safe_error_text(str(exc))],
                    "routing_status": "fallback_both",
                }
                print(
                    "警告：规则自动分流失败，已安全降级为视觉轨和音频轨均使用完整规则；"
                    "最终结果将要求人工确认分流。",
                    flush=True,
                )
            required_keys = {
                "visual_requirements",
                "audio_requirements",
                "combined_requirements",
                "manual_or_system_requirements",
                "uncertain_items",
            }
            string_keys = required_keys - {"uncertain_items"}
            if (
                not required_keys.issubset(routing)
                or not isinstance(routing["uncertain_items"], list)
                or any(not isinstance(routing[key], str) for key in string_keys)
                or any(not isinstance(item, str) for item in routing["uncertain_items"])
            ):
                raise PreflightError("审核规则自动分流返回格式无效。")
            routing.setdefault("routing_status", "success")
            engine.ACTIVE_AUDIT_CONFIG["rule_routing_result"] = routing
            print("审核规则已由文本模型分流为视觉、音频、综合和人工/系统要求。", flush=True)

        def call_qwen_with_track_key(*args, **kwargs):
            if kwargs.get("video_path") is not None:
                kwargs["api_key"] = credentials["vision"]
                return original_call_qwen(*args, **kwargs)
            return call_text_generation(
                engine,
                prompt=kwargs["prompt"],
                api_key=credentials["text"],
                model=text_model,
                api_base=kwargs["api_base"],
            )

        def transcribe_with_asr_key(*args, **kwargs):
            kwargs["api_key"] = credentials["asr"]
            return original_transcribe(*args, **kwargs)

        engine.call_qwen = call_qwen_with_track_key
        engine.transcribe_audio_chunks = transcribe_with_asr_key

    def audit_video_with_basis(*args, **kwargs):
        result = original_audit_video(*args, **kwargs)
        result["audit_basis"] = basis
        if engine.ACTIVE_AUDIT_CONFIG.get("mode") == "simple":
            result["rule_routing"] = engine.ACTIVE_AUDIT_CONFIG.get("rule_routing_result", {})
        if credentials:
            result["models"]["audio_rule_analysis"] = text_model
            result["audio"]["analysis_model"] = text_model
            result["parameters"]["api_key_mode"] = "ASR/视觉/文本三轨独立密钥"
        return result

    engine.audit_video = audit_video_with_basis


def pop_text_model(argv: list[str]) -> tuple[list[str], str | None]:
    """取出包装层的 --text-model，避免传给旧引擎参数解析器。"""
    updated = [argv[0]]
    text_model = None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--text-model":
            if index + 1 >= len(argv):
                raise PreflightError("--text-model 后必须填写模型名称。")
            text_model = argv[index + 1].strip()
            index += 2
            continue
        if arg.startswith("--text-model="):
            text_model = arg.split("=", 1)[1].strip()
            index += 1
            continue
        updated.append(arg)
        index += 1
    if text_model == "":
        raise PreflightError("--text-model 不能为空。")
    return updated, text_model


def load_track_config(engine, require_keys: bool) -> tuple[dict[str, str] | None, dict[str, str]]:
    engine.load_dotenv(ENV_PATH)
    provider = os.getenv("MODEL_PROVIDER", "dashscope").strip().lower()
    if provider != "dashscope":
        raise PreflightError(
            f"当前安装仅内置 dashscope 适配器，尚不支持 MODEL_PROVIDER={provider}。"
        )
    models = {
        "asr": os.getenv("ASR_MODEL", engine.DEFAULT_ASR_MODEL).strip(),
        "vision": os.getenv("VISION_MODEL", engine.DEFAULT_VISUAL_MODEL).strip(),
        "text": os.getenv("TEXT_MODEL", DEFAULT_TEXT_MODEL).strip(),
    }
    if not all(models.values()):
        raise PreflightError(".env 中的模型名称不能为空。")
    if not require_keys:
        return None, models

    credentials = {
        "asr": os.getenv("ASR_API_KEY", "").strip(),
        "vision": os.getenv("VISION_API_KEY", "").strip(),
        "text": os.getenv("TEXT_API_KEY", "").strip(),
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        labels = {"asr": "ASR_API_KEY", "vision": "VISION_API_KEY", "text": "TEXT_API_KEY"}
        raise PreflightError(
            f"请在 {ENV_PATH} 中填写：" + "、".join(labels[name] for name in missing)
        )

    # 只用于通过旧引擎的非空校验；实际调用会按轨道替换为对应密钥。
    os.environ["DASHSCOPE_API_KEY"] = credentials["vision"]
    return credentials, models


def has_option(argv: list[str], name: str) -> bool:
    return any(arg == name or arg.startswith(f"{name}=") for arg in argv)


def config_path_from_argv(argv: list[str]) -> Path:
    for index, arg in enumerate(argv):
        if arg == "--config":
            if index + 1 >= len(argv):
                raise PreflightError("--config 后必须填写配置文件路径。")
            return Path(argv[index + 1]).expanduser()
        if arg.startswith("--config="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise PreflightError("--config 不能为空。")
            return Path(value).expanduser()
    return DEFAULT_CONFIG_PATH


def settings_path_from_argv(argv: list[str]) -> Path:
    for index, arg in enumerate(argv):
        if arg == "--settings":
            if index + 1 >= len(argv):
                raise PreflightError("--settings 后必须填写设置文件路径。")
            return Path(argv[index + 1]).expanduser()
        if arg.startswith("--settings="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise PreflightError("--settings 不能为空。")
            return Path(value).expanduser()
    return PROJECT_DIR / "settings.json"


def inject_defaults(argv: list[str], models: dict[str, str]) -> list[str]:
    updated = list(argv)
    if not has_option(updated, "--config"):
        updated.extend(["--config", str(DEFAULT_CONFIG_PATH)])
    if not has_option(updated, "--output-dir"):
        updated.extend(["--output-dir", str(PROJECT_DIR / "results")])
    if not has_option(updated, "--asr-model"):
        updated.extend(["--asr-model", models["asr"]])
    if not has_option(updated, "--visual-model"):
        updated.extend(["--visual-model", models["vision"]])
    return updated


def main() -> int:
    try:
        enter_local_venv()
        engine = load_engine()
        cleaned_argv, cli_text_model = pop_text_model(sys.argv)
        engine.DEFAULT_SETTINGS_PATH = settings_path_from_argv(cleaned_argv)
        basis = validate_basis(engine, config_path_from_argv(cleaned_argv))
        non_api_mode = any(
            option in cleaned_argv
            for option in ("--self-test", "--prepare-only", "--validate-config", "--help", "-h")
        )
        credentials, models = load_track_config(engine, require_keys=not non_api_mode)
        if credentials and engine.ACTIVE_AUDIT_CONFIG.get("template_incomplete"):
            raise PreflightError(
                "rules/review_brief.md 仍是示例模板。请删除 REVIEW_RULES_REQUIRED "
                "标记并填写真实审核要求后再运行。"
            )
        if credentials and engine.script_comparison_enabled():
            for scenario in engine.SCENARIOS.values():
                script_value = str(scenario.get("script", ""))
                if not script_value:
                    continue
                script_path = config_path_from_argv(cleaned_argv).parent / script_value
                if "SCRIPT_RULES_REQUIRED" in script_path.read_text(encoding="utf-8"):
                    raise PreflightError(
                        f"话术文件仍是示例模板：{script_path}。请填写后再运行。"
                    )
        if cli_text_model:
            models["text"] = cli_text_model
        configure_engine(engine, basis, credentials, models["text"])

        sys.argv = inject_defaults(cleaned_argv, models)
        print(
            f"审核配置检查通过：{basis['rule_count']}项规则；默认场景由 settings.json 决定。",
            flush=True,
        )
        print("运行模式：视觉轨与音频轨独立处理，最终由本地规则合并。", flush=True)
        if credentials:
            print(
                "密钥配置：ASR、视觉、文本三轨已分别加载（密钥内容不会输出）。",
                flush=True,
            )
        return int(engine.main())
    except PreflightError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
