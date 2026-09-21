# 淘宝女装 AI 智能剪辑 Agent 源码审计与 P0 实施记录

审计日期：2026-09-21

## 一、真实入口

- 桌面快捷方式：`AI 智能剪辑.lnk`。
- 启动脚本：`启动SOCHEN合并版.ps1`。
- 后台守护：`SOCHEN后台服务.ps1`。
- 统一网站：FastAPI `fulin_editor.web_api:app`，端口 8080。
- 原剪辑引擎：`suchen-server.exe`，端口 5000。
- Web 前端：`integration/suchen-ui/templates` 与 `integration/suchen-ui/static`。
- 统一数据库：运行目录 `便携数据/unified/fulin_editor.sqlite3`。
- 原引擎记忆库：`便携数据/runtime/memory/suchen_memory.sqlite3`，统一网站以桥接方式读取。
- 测试入口：`python -m pytest tests -q`。

## 二、当前调用链

1. 用户通过 8080 网站登录并打开 SOCHEN 工作台。
2. 前端上传或选择视频，读取动作模板与知识规则。
3. FastAPI 处理账号、知识库、Web任务、文件边界和原引擎桥接。
4. SOCHEN 原引擎执行 ASR、动作分析、计划、渲染和原质量报告。
5. 统一网站读取并规范化原报告，区分通过、待复核和失败。
6. SQLite 保存用户、任务、知识库、历史桥接和本轮新增的 Agent 运行轨迹。

## 三、审计确认的已有能力

- 登录、注册、角色和签名会话。
- 上传、任务队列、成片预览与下载。
- SOCHEN 原剪辑方法保持不变。
- faster-whisper 口播识别接口。
- MediaPipe/OpenCV 动作与画面分析接口。
- FFmpeg/FFprobe 3:4 原声输出与媒体检查。
- 单品、套装、大货品类时长规则。
- 知识库规则管理和动作模板同步。
- 连续硬剪兜底不得冒充智能剪辑通过。
- 黑帧、整帧闪现、同景别手势变化区分逻辑。
- 本地 SQLite、桌面启动和公网 HTTPS 入口。

## 四、审计发现的主要缺口

- 原任务结果缺少统一 Agent Run 和逐阶段审计记录。
- 计划版本、质量结果和负责人反馈没有统一关联。
- 桌面主工作台和 Web 任务接口仍存在两条任务入口，需要继续统一 Run ID。
- 现阶段没有“失败项 → 新候选区间 → 只替换失败阶段”的完整重剪闭环。
- 缺少至少 20 条负责人标注的固定评测集，因此不能宣称自动重剪准确率。
- 剪映 Y=500 已记录为工程位置参数，但仍缺真实剪映参考工程的自动校准验收。

## 五、本次完成的 P0

新增数据库表：

- `agent_runs`
- `agent_events`
- `agent_plan_versions`
- `agent_quality_results`
- `human_feedback`

新增能力：

- 每个 Web 剪辑任务创建独立 Agent Run。
- 映射 `RECEIVED / ANALYZING / PLANNING / VALIDATING / RENDERING / EVALUATING / PASSED / NEEDS_REVIEW` 状态。
- 保存阶段输入输出摘要、耗时、错误和规则版本。
- 保存第一版剪辑计划和质量报告。
- 工具失败停止在 `NEEDS_REVIEW`，不伪造通过。
- 新增 `GET /api/jobs/{job_id}/agent-trace` 查询运行轨迹。
- 新增 `POST /api/jobs/{job_id}/feedback` 保存负责人评价和人工切点。
- 负责人反馈做角色校验、问题类型白名单和时间轴正序/重叠校验。

## 六、未冒充完成的部分

- Agent 内核已经支持最多两轮有限重试；目前只有时长边界和渲染器失败属于可确定修正项。闪现、缺尺码、缺面料、缺背面等没有安全替代证据时会停止待复核，不会重复同一算法冒充纠错。
- 原 SOCHEN 引擎仍需暴露候选片段与可替换阶段接口，才能把更多质量失败转换为“只替换失败阶段”的安全重剪。
- P2 合格案例检索尚未接入。负责人反馈已经保存，但只有后续明确确认和建库后才能进入 `golden_examples`。
- 原 SOCHEN 兼容入口仍保留独立历史状态；主工作台按钮已改走统一 Agent Run，旧历史记录尚未反向迁移。
- 真实视频通过率、误报率、漏报率和一分钟处理目标均需固定评测集实测。

## 七、验证记录

- Python 语法编译：通过。
- 自动测试：6 passed。
- 网站重启后本机健康检查：`status=ok`。
- 公网登录页：HTTP 200。
- 运行数据库确认五张 Agent 表均已创建。
- FastAPI 仍有 `on_event` 弃用警告，不影响当前运行，后续迁移 lifespan。
- 新增真实工具型 Agent 命令 `fulin-agent` 和 API `POST /api/agent/jobs`。
- 主工作台“当前视频”和“批量”按钮默认调用 Agent API；新增 `POST /api/agent/jobs/batch`，页面刷新后可恢复活动 Agent 任务与已生成成片。
- 页面显示 Agent 有限状态机、计划版本、质检轮次和人工接管结论；原 SOCHEN 仅作为兼容能力保留。
- 使用真实素材 `VID67Q056.mp4` 验收：60.311秒、36句口播、120帧动作采样、人物检测率0.8333；规划发现52.12秒之后缺少满足背面转回要求的候选区间，因此40.024秒后安全停止为 `NEEDS_REVIEW`，没有虚构成片。

## 八、下一实施顺序

1. 从原引擎报告建立“阶段候选池”和证据 ID。
2. 为缺失阶段、闪现、时长和音频问题定义更多确定性的修正规则。
3. 将 Agent 人工反馈表单完整接入主工作台问题类型与人工切点。
4. 用 20 条人工标注真实素材建立回归评测集。
