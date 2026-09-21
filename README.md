# 淘宝女装 AI 智能剪辑 Agent

这是一个面向淘宝女装直播原片的可审计剪辑 Agent。系统会基于真实口播、人物动作、景别和画质证据，依次执行分析、规划、验证、渲染、质检和有限自动修正；无法确认时进入人工复核，不伪造成功。

## Agent 闭环

`RECEIVED → ANALYZING → PLANNING → VALIDATING → RENDERING → EVALUATING → PASSED`

质检失败后最多修正两轮，仍不合格进入 `NEEDS_REVIEW`。

## 技术组成

- FastAPI：任务、账号、知识库和 Agent API
- SQLite：任务、事件、计划版本、质检与人工反馈
- faster-whisper：中文口播和时间戳
- MediaPipe / OpenCV：人物动作与画面证据
- FFmpeg：原速、原声、3:4 成片渲染
- HTML / CSS / JavaScript：黑紫色工作台与审计界面

## 本地运行

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
uvicorn fulin_editor.web_api:app --host 127.0.0.1 --port 8080
```

打开 `http://127.0.0.1:8080/`。

## 测试

```powershell
$env:PYTHONPATH=(Resolve-Path .\src).Path
python -m pytest -q
```

## 隐私与仓库边界

仓库不包含公司原视频、成片、用户数据库、登录信息、访问令牌、隧道配置和下载的模型二进制。运行数据应保存在 `.gitignore` 已排除的目录中。

完整业务规则见 `prompts/TAOBAO_WOMEN_VIDEO_AGENT_PROMPT_V3.md`，实现与验收现状见 `SOURCE_AUDIT_2026-09-21.md`。
