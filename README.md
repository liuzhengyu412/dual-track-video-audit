# 视频音频双轨审核

把审核规则交给视觉模型、语音转写模型和文本模型，生成视觉审核、音频审核、综合审核以及待人工复核清单。支持直接粘贴整段规则，也支持逐条配置规则。

> 本项目用于辅助审核，不替代最终人工或业务系统判断。报告默认保留审核所需真实信息，请按敏感资料保管。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

然后完成三件事：

1. 在 `.env` 填写三个模型角色的 API Key 和模型名称。
2. 把真实审核要求粘贴到 `rules/review_brief.md`，删除文件中的 `REVIEW_RULES_REQUIRED` 标记。
3. 运行 `python3 run_audit.py /绝对路径/视频.mp4`。

结果保存在 `results/`，包括单个视频的 JSON 和多个视频的 Excel 汇总。

## 三个模型分别做什么

| 角色 | 用途 |
| --- | --- |
| `VISION_MODEL` | 查看视频画面，生成视觉审核结果。 |
| `ASR_MODEL` | 把完整音频转成带时间戳的文字。 |
| `TEXT_MODEL` | 简易模式下先分流规则，再根据 ASR 转写生成音频审核结果。 |

综合审核不调用第四个模型。程序按“风险优先、证据不足转人工”的方式合并视觉和音频结果。它能保留任一轨道发现的风险，但暂时不能直接比较画面中的某个值和语音中的某个值。

## 简易模式和高级模式

### simple：直接粘贴整段规则

默认模式。使用者不需要自己拆分规则，`TEXT_MODEL` 会先把整段内容分为视觉要求、音频要求、综合要求、人工/系统要求和无法可靠分类的要求。

人工/系统要求和无法分类要求会进入 JSON、Excel 的“规则分流与人工项”和“待复核清单”，不会被静默忽略。

如果规则分流失败，默认安全降级为视觉轨和音频轨都使用完整规则，并额外生成一项人工复核提示。可在 `settings.json` 改为失败即停止。

### advanced：逐条配置规则

适合需要每条规则独立统计、指定证据轨道或接入本地校验器的项目。把 `settings.json` 的 `mode` 改为 `advanced`，并在 `rules/audit_config.json` 添加：

```json
"rules": [
  {
    "id": "consent_confirmed",
    "category": "流程",
    "name": "已完成同意确认",
    "method": "文本模型",
    "tracks": ["audio"],
    "instruction": "检查对方是否明确表示同意；无法确认时人工复核。"
  }
]
```

`tracks` 支持 `visual`、`audio`、`system`，也可同时填写 `visual` 和 `audio`。规则 ID 只需唯一，不要求使用 `1.1`、`2.1` 等编号。

## 话术比对

默认关闭，不需要填写或保留话术文件：

```json
"script_comparison": {"enabled": false}
```

需要时改为 `true`，在 `rules/scripts/example_review.md` 填写必问项、必须告知事项和可接受表达，并删除 `SCRIPT_RULES_REQUIRED` 标记。文本模型会根据真实转写逐项比对。

## settings.json

普通使用者主要修改这个文件：

```json
{
  "mode": "simple",
  "default_scenario": "example_review",
  "rule_routing": "ai",
  "routing_failure_strategy": "fallback_both",
  "script_comparison": {"enabled": false},
  "speaker_roles": ["staff", "customer", "unknown"],
  "limits": {"max_transcript_characters": 60000},
  "processing": {
    "fps": 2.0,
    "audio_chunk_seconds": 180,
    "confidence_threshold": 0.7,
    "max_video_size_mb": 100,
    "keep_work_files": false
  },
  "output": {
    "include_transcript": true,
    "create_empty_script_sheet": false
  }
}
```

| 设置 | 含义 |
| --- | --- |
| `mode` | `simple` 直接粘贴规则；`advanced` 逐条配置。 |
| `default_scenario` | 未传 `--scenario` 时使用的场景。 |
| `routing_failure_strategy` | `fallback_both` 安全降级；`stop` 立即停止。 |
| `script_comparison.enabled` | 是否启用标准话术比对。 |
| `speaker_roles` | 可用说话人标签，可改为客服、客户、主持人等角色；程序会保留 `unknown`。 |
| `max_transcript_characters` | 防止超长转写被截断；超过后音频规则转人工复核。 |
| `processing` | 抽帧率、音频切分、置信度、视频大小和临时文件开关。 |
| `include_transcript` | 是否把原始转写和角色标注写入 Excel。 |
| `create_empty_script_sheet` | 话术关闭时是否仍建立空工作表。 |

可以把另一套设置和规则一起使用：

```bash
python3 run_audit.py video.mp4 --settings project-a/settings.json --config project-a/audit_config.json
```

## 需要修改的文件

| 文件 | 用途 |
| --- | --- |
| `.env` | API Key、模型名称和模型服务地址，不得提交。 |
| `settings.json` | 模式、话术、角色、处理参数和输出开关。 |
| `rules/review_brief.md` | 简易模式的完整审核要求。 |
| `rules/audit_config.json` | 场景、话术、提示词路径；高级模式还在这里填写规则。 |
| `rules/scripts/*.md` | 仅启用话术比对时填写。 |
| `prompts/*.md` | AI 的通用审核方法；通常无需修改，不要删除 `{{变量}}`。 |

## 模型服务说明

规则、提示词和结果结构与业务场景无关，但当前仓库只内置 `dashscope` 模型服务适配器。`.env` 中的 `MODEL_PROVIDER` 目前应保持为 `dashscope`。接入其他厂商时，应在模型适配层实现相同的视觉、文本和 ASR 接口，不需要修改审核规则与报表结构。

## 数据与安全

- 视频发送给视觉模型；音频发送给 ASR；转写和审核要求发送给文本模型。请确保有权处理这些数据。
- 报告保留真实姓名、号码、转写和其他审核所需信息，不做业务内容脱敏。
- API Key 不写入报告；异常信息中的常见 Key 形式会被遮盖。
- `.gitignore` 已忽略 `.env`、视频、`work/` 和审核结果，提交前仍应检查 Git 暂存区。
- Excel 会转义可能被当成公式执行的文本开头，不改变审核内容。

## 检查与测试

```bash
python3 run_audit.py --validate-config
python3 run_audit.py --self-test
python3 run_audit.py /绝对路径/视频.mp4 --prepare-only
python3 -m unittest discover -s tests -v
```

## 代码结构

```text
run_audit.py        运行入口与三模型角色配置
audit_pipeline.py   配置加载、提示词构建和流程编排
common.py           公共类型、状态和工具
media.py            音频提取、切分和基础指标
models.py           模型服务适配与 ASR 时间轴恢复
results.py          结果校验、双轨合并和人工复核项
reporting.py        JSON 与 Excel 报告
prompts/            独立 AI 提示词
rules/              审核规则、场景和话术
tests/              不调用外部 API 的自动化测试
```

项目采用 MIT License。提交改进前请阅读 `CONTRIBUTING.md`，安全与真实数据注意事项见 `SECURITY.md`。
