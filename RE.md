# Fastbot 卡顿帧监控技术方案

## 1. 背景

当前 Fastbot 已具备以下能力：

- 支持任务级开关控制 `性能监控` 与 `卡顿帧监控`
- 已实现 CPU / 内存采样，并在报告页展示
- `卡顿帧监控` 仅有开关透传，尚未实现实际监控链路

当前 Fastbot 运行主链路位于：

- 任务执行入口：`backend/api/fastbot.py`
- 监控与汇总主逻辑：`backend/fastbot_runner.py`
- 报告落库模型：`backend/models.py`
- 报告详情页：`frontend/src/views/fastbot/FastbotReportDetail.vue`

本方案目标是补齐“卡顿帧监控”闭环，并且尽量复用现有 Fastbot 监控框架，而不是再单独起一套系统。


## 2. 目标

### 2.1 核心目标

- 仅在 `enable_jank_frame_monitor=true` 时启用卡顿监控
- 在 Fastbot 跑测过程中持续产出可视化的流畅度数据
- 在出现严重卡顿时保留可用于归因的证据
- 报告中同时展示“趋势数据”和“异常事件”
- 后续可接入 LLM，输出结构化根因解释

### 2.2 非目标

- 不追求第一版即覆盖所有 Android 版本的完整逐帧归因
- 不要求第一版就做到 Perfetto 级别的全量自动诊断
- 不把 LLM 作为唯一判断依据，LLM 仅负责解释，不负责裁决


## 3. 总体思路

建议采用“三层架构”，但要调整为“轻量触发器 + 证据保全 + 结构化分析”的实现方式：

1. 轻量触发器层
   使用 `gfxinfo`/`framestats` 类轻量采样做巡检，成本低，适合全程运行。

2. 证据保全层
   对支持的设备在后台维持低开销 Perfetto ring buffer；触发严重卡顿时导出 trace。

3. 分析归因层
   先用规则和 SQL 生成结构化结论，再把结构化结果交给 LLM 做中文总结与建议。

一句话概括：

`gfxinfo` 负责“发现异常”，Perfetto 负责“保留现场”，SQL/规则负责“形成结论”，LLM 负责“把结论讲清楚”。


## 4. 推荐实现策略

### 4.1 真相源选择

不建议把 `dumpsys gfxinfo` 直接当作最终归因依据。

建议分层处理：

- Android 12+：
  优先使用 Perfetto FrameTimeline 作为高可信证据源
- Android 11 及以下：
  使用 `gfxinfo` 作为 fallback，只提供趋势和粗粒度告警

原因：

- `gfxinfo` 适合做低成本巡检，但精度和稳定性受系统版本、厂商实现、刷新率影响较大
- Perfetto `actual_frame_timeline_slice` 更适合做逐帧分析与卡顿归因
- 高刷设备下，固定使用“卡顿率 > 15%”这类阈值容易失真


## 5. 与当前代码的集成位置

建议直接沿用当前 Fastbot 的监控任务模型，不新增独立执行器。

### 5.1 运行链路

当前 `run_fastbot_task()` 已经通过 `monitor_tasks` 管理并发监控协程，适合集成卡顿监控：

- `backend/fastbot_runner.py`

建议新增：

- `_monitor_jank(...)`
- `_start_perfetto_ring_buffer(...)`
- `_export_perfetto_trace(...)`
- `_analyze_jank_trace(...)`

其中 `_monitor_jank()` 与 `_monitor_performance()` 并列挂入 `monitor_tasks`。

### 5.2 报告链路

当前 `FastbotReport` 仅保存：

- `performance_data`
- `crash_events`
- `summary`

建议扩展为：

- `jank_data`
- `jank_events`
- `trace_artifacts`
- `summary` 中补充卡顿汇总字段


## 6. 数据模型设计

### 6.1 FastbotReport 建议新增字段

建议在 `backend/models.py` 的 `FastbotReport` 中新增以下 JSON 字段：

```python
jank_data: Optional[str] = None
jank_events: Optional[str] = None
trace_artifacts: Optional[str] = None
```

### 6.2 字段结构建议

`jank_data`

```json
[
  {
    "time": "10:05:10",
    "window_sec": 5,
    "fps": 42.3,
    "jank_rate": 0.12,
    "total_frames": 251,
    "jank_frames": 31,
    "slow_frames": 7,
    "frozen_frames": 0,
    "source": "gfxinfo"
  }
]
```

`jank_events`

```json
[
  {
    "time": "10:05:10",
    "severity": "CRITICAL",
    "reason": "HIGH_JANK_RATE",
    "fps": 22.1,
    "jank_rate": 0.26,
    "window_sec": 5,
    "trace_exported": true,
    "trace_path": "reports/fastbot/32/jank_trace_001.perfetto-trace",
    "diagnosis_status": "PENDING"
  }
]
```

`trace_artifacts`

```json
[
  {
    "path": "reports/fastbot/32/jank_trace_001.perfetto-trace",
    "trigger_time": "10:05:10",
    "trigger_reason": "CRITICAL_JANK",
    "analyzed": false
  }
]
```

### 6.3 summary 建议新增字段

```json
{
  "jank_frame_monitor_enabled": true,
  "avg_fps": 53.8,
  "min_fps": 21.4,
  "max_jank_rate": 0.26,
  "avg_jank_rate": 0.08,
  "total_jank_events": 3,
  "severe_jank_events": 1,
  "frame_timeline_supported": true
}
```


## 7. 第一阶段实现方案：轻量巡检 MVP

### 7.1 目标

先不依赖 Perfetto，也能完成以下闭环：

- 跑测期间持续采样卡顿数据
- 报告展示 FPS / 卡顿率趋势
- 报告展示严重卡顿事件
- 为后续 Perfetto 取证预留触发器和数据结构

### 7.2 监控方式

新增 `_monitor_jank()` 协程，逻辑与 `_monitor_performance()` 保持一致：

```python
async def _monitor_jank(
    device_serial: str,
    package_name: str,
    stop_event: asyncio.Event,
    jank_data: List[Dict],
    jank_events: List[Dict],
    interval: int = 5,
):
    ...
```

建议流程：

1. 每 5 秒采集一次 `gfxinfo/framestats`
2. 解析出窗口内：
   - `fps`
   - `total_frames`
   - `jank_frames`
   - `slow_frames`
   - `frozen_frames`
   - `jank_rate`
3. 追加到 `jank_data`
4. 命中阈值时写入 `jank_events`

### 7.3 阈值设计

不建议只用单一条件 `jank_rate > 0.15`。

建议使用双级告警：

`WARNING`

- 连续 2 个窗口 `jank_rate >= 0.08`
- 或连续 2 个窗口 `fps < 45`

`CRITICAL`

- 连续 2 个窗口 `jank_rate >= 0.15`
- 或连续 2 个窗口 `fps < 30`
- 或单窗口出现 `frozen_frames > 0`

### 7.4 去重与冷却

必须加状态机，避免频繁触发：

- 连续窗口计数器
- 冷却时间：60 秒
- 单任务最大导出次数：3 次
- 同类型事件 30 秒内不重复写入

否则 Monkey 在复杂页面上会非常容易反复触发导出。


## 8. 第二阶段实现方案：Perfetto 证据保全

### 8.1 适用范围

仅在以下条件满足时启用：

- `enable_jank_frame_monitor = true`
- 设备支持 Perfetto / FrameTimeline
- Perfetto session 启动成功

若任一条件不满足，则自动降级为“仅 `gfxinfo` 巡检模式”。

### 8.2 推荐模式

建议不是“触发时才开始录 Perfetto”，而是：

- 任务开始时启动低开销 ring buffer
- 严重卡顿时导出最近一段 trace

原因：

- 触发后再开始录制，往往拿不到卡顿前的现场
- ring buffer 可以保留前溯证据，更适合事后归因

### 8.3 触发策略

当 `_monitor_jank()` 识别到 `CRITICAL` 级事件时：

1. 检查当前是否处于导出冷却期
2. 若否，则触发 `_export_perfetto_trace()`
3. 记录导出结果到 `trace_artifacts`
4. 在对应 `jank_event` 上写入 `trace_path`

### 8.4 风险与兜底

- Perfetto 启动失败：只记录 warning，不中断 Fastbot 主任务
- Trace 导出失败：事件保留，但 `trace_exported=false`
- 设备不支持 FrameTimeline：summary 写明 `frame_timeline_supported=false`


## 9. 第三阶段实现方案：结构化归因

### 9.1 原则

LLM 只做解释，不做原始判断。

推荐输出链路：

1. Perfetto SQL / 规则引擎产出结构化事实
2. 结构化事实写入分析结果
3. LLM 基于结构化事实生成中文诊断结论

### 9.2 SQL 设计原则

不建议直接使用“无时间窗、无包过滤”的 SQL。

必须至少补齐：

- 卡顿事件触发时间窗
- 目标 App 过滤
- 目标 layer / process 过滤
- Top N 最慢帧
- 同时间窗的主线程 / RenderThread / JS 线程耗时

### 9.3 SQL 示例

以下为“示意 SQL”，实际字段需根据 trace schema 和采集配置适配：

```sql
SELECT
  ts,
  dur / 1e6 AS duration_ms,
  jank_type,
  layer_name
FROM actual_frame_timeline_slice
WHERE ts BETWEEN :start_ts AND :end_ts
  AND jank_type != 'None'
  AND layer_name LIKE :package_like
ORDER BY dur DESC
LIMIT 20;
```

如果要进一步归因，可追加：

- 主线程切片耗时
- RenderThread 忙碌区间
- Binder / GPU / IO 峰值区间
- React Native / Flutter / WebView 线程切片

### 9.4 LLM 输入建议

建议传给 LLM 的不是原始 trace，而是裁剪后的结构化摘要：

```json
{
  "event_time": "10:05:10",
  "fps": 22.1,
  "jank_rate": 0.26,
  "top_jank_frames": [...],
  "top_busy_threads": [...],
  "cpu_spikes": [...],
  "render_thread_summary": "...",
  "app_stack_hints": ["React Native"]
}
```

LLM 输出内容建议限定为：

- 问题发生时间
- 现象摘要
- 最可能原因
- 次要怀疑点
- 建议排查方向


## 10. 前后端改造建议

### 10.1 后端

需要新增：

- `backend/fastbot_runner.py`
  - `_monitor_jank`
  - `_compute_jank_summary`
  - `_start_perfetto_ring_buffer`
  - `_export_perfetto_trace`
  - `_analyze_jank_trace`

- `backend/models.py`
  - `FastbotReport` 新增 `jank_data` / `jank_events` / `trace_artifacts`

- `backend/schemas.py`
  - `FastbotReportRead` 增加上述三个字段

- `backend/api/fastbot.py`
  - 报告接口补充返回这些字段

### 10.2 前端

建议在 `FastbotReportDetail.vue` 中新增：

- 卡顿趋势图
  - FPS 曲线
  - 卡顿率曲线

- 卡顿事件表
  - 时间
  - 等级
  - FPS
  - 卡顿率
  - 是否已导出 trace
  - 分析状态

- 卡顿汇总卡片
  - 平均 FPS
  - 最低 FPS
  - 最大卡顿率
  - 严重卡顿次数

- Trace 入口
  - 下载 trace
  - 查看 AI 诊断摘要


## 11. 分阶段落地计划

### Phase A：MVP

目标：

- 实现 `gfxinfo` 采样
- 报告中展示 `jank_data` / `jank_events`
- summary 中展示 FPS / 卡顿率统计

不做：

- Perfetto
- LLM

### Phase B：证据保全

目标：

- 启动 Perfetto ring buffer
- 严重卡顿自动导出 trace
- 报告里可下载 trace 文件

### Phase C：结构化分析

目标：

- 基于 trace 做 SQL 分析
- 输出结构化诊断结果
- 在报告页展示卡顿根因摘要

### Phase D：LLM 增强

目标：

- 在结构化结果基础上生成自然语言报告
- 输出工程化排查建议


## 12. 测试建议

### 12.1 单元测试

- `gfxinfo` 输出解析
- 卡顿阈值状态机
- 冷却时间与去重逻辑
- summary 汇总逻辑
- 报告 JSON 序列化

### 12.2 集成测试

- 开启卡顿监控时，报告有 `jank_data`
- 关闭卡顿监控时，报告没有卡顿监控数据
- 设备不支持 Perfetto 时，任务不中断且能正常降级
- trace 导出失败时，主任务仍标记为完成

### 12.3 回归测试

- 不影响现有 CPU / 内存监控
- 不影响 Crash / ANR 统计
- 不影响报告页已有性能展示逻辑


## 13. 风险与规避

### 风险 1：设备兼容性差

规避：

- 明确区分 `gfxinfo` 巡检模式和 Perfetto 深度分析模式
- 在 summary 中标记当前模式和支持状态

### 风险 2：触发过于频繁

规避：

- 连续窗口判定
- 冷却时间
- 单任务最大导出次数

### 风险 3：trace 太大、分析太慢

规避：

- 只在 `CRITICAL` 事件时导出
- 限制 ring buffer 大小
- 每个任务限制导出次数
- 分析时仅截取相关时间窗

### 风险 4：LLM 误判

规避：

- 先规则归因，再 LLM 总结
- LLM 输入必须为结构化事实，不直接喂原始 trace


## 14. 最终建议

如果希望方案尽快落地，建议按下面顺序推进：

1. 先做 `Phase A`
   先把卡顿趋势和异常事件打通，最快见到业务价值。

2. 再做 `Phase B`
   把“发现问题”升级为“保留现场”。

3. 最后做 `Phase C / D`
   把 trace 分析与 LLM 解释叠上去，形成完整诊断链路。

这样推进的好处是：

- 每一阶段都可独立验收
- 不会把 Perfetto 和 LLM 一次性压进首版
- 与当前 Fastbot 的监控框架耦合最小，改造最稳


## 15. 参考资料

- AndroidX Macrobenchmark `FrameTimingGfxInfoMetric`
  - https://developer.android.com/reference/kotlin/androidx/benchmark/macro/FrameTimingGfxInfoMetric
- AndroidX Macrobenchmark `FrameTimingMetric`
  - https://developer.android.com/reference/kotlin/androidx/benchmark/macro/FrameTimingMetric
- Perfetto FrameTimeline
  - https://perfetto.dev/docs/data-sources/frametimeline


## 16. 开发任务拆解

下面按“建议实施顺序”拆成可以直接执行的开发任务。

### Task 1：补齐报告数据模型

目标：

- 为卡顿监控新增持久化字段和返回 schema

涉及文件：

- `backend/models.py`
- `backend/schemas.py`
- `backend/api/fastbot.py`
- `backend/database.py`

改动内容：

- `FastbotReport` 新增：
  - `jank_data`
  - `jank_events`
  - `trace_artifacts`
- `FastbotReportRead` 新增对应字段
- `/fastbot/reports/{task_id}` 返回这三块数据
- 增加 SQLite migration，为已有表补列

验收标准：

- 数据库迁移后旧数据不丢失
- 新报告接口返回结构中包含 `jank_data` / `jank_events` / `trace_artifacts`
- 老报告兼容为空数组或空对象


### Task 2：实现 jank 轻量巡检协程

目标：

- 跑测期间持续采样卡顿趋势数据

涉及文件：

- `backend/fastbot_runner.py`
- `backend/tests/`

改动内容：

- 新增 `_monitor_jank(...)`
- 新增 `gfxinfo/framestats` 输出解析函数
- 在 `run_fastbot_task()` 中，当 `enable_jank_frame_monitor=true` 时挂入 `monitor_tasks`
- 产出：
  - `jank_data`
  - `jank_events`

验收标准：

- 开启卡顿监控时，任务执行后报告里能看到 `jank_data`
- 关闭卡顿监控时，报告里无卡顿趋势数据
- 卡顿监控异常不会中断 Fastbot 主任务


### Task 3：实现卡顿事件判定状态机

目标：

- 避免误报和高频重复触发

涉及文件：

- `backend/fastbot_runner.py`
- `backend/tests/`

改动内容：

- 实现 `WARNING` / `CRITICAL` 双级判定
- 引入：
  - 连续窗口计数器
  - 冷却时间
  - 单任务最大导出次数
  - 同类事件去重

验收标准：

- 单窗口偶发抖动不会触发 `CRITICAL`
- 连续窗口卡顿可稳定触发事件
- 同一段卡顿不会在短时间内重复生成多条相同事件


### Task 4：补齐卡顿汇总逻辑

目标：

- 报告 summary 中展示卡顿关键指标

涉及文件：

- `backend/fastbot_runner.py`
- `backend/tests/`

改动内容：

- 新增 `_compute_jank_summary(...)`
- 合并进现有 `summary`
- 建议输出：
  - `avg_fps`
  - `min_fps`
  - `avg_jank_rate`
  - `max_jank_rate`
  - `total_jank_events`
  - `severe_jank_events`
  - `frame_timeline_supported`

验收标准：

- 报告详情页可直接消费 summary，不需要前端自己重新统计
- 无卡顿数据时字段有明确默认值


### Task 5：前端报告页展示卡顿信息

目标：

- 在现有 Fastbot 报告中展示卡顿监控结果

涉及文件：

- `frontend/src/views/fastbot/FastbotReportDetail.vue`

改动内容：

- 新增卡顿汇总卡片
- 新增卡顿趋势图：
  - FPS 曲线
  - 卡顿率曲线
- 新增卡顿事件表
- 在任务信息中展示：
  - `卡顿帧监控：已开启/已关闭`
  - `当前模式：gfxinfo / Perfetto`

验收标准：

- 开启卡顿监控后可看到趋势与事件
- 关闭卡顿监控后卡顿模块不展示
- 不影响已有 CPU / 内存 / Crash / ANR 展示


### Task 6：实现 Perfetto ring buffer 启动与导出

目标：

- 为严重卡顿保留可分析证据

涉及文件：

- `backend/fastbot_runner.py`
- 可新增：
  - `backend/perfetto_runner.py`
  - `backend/tests/`

改动内容：

- 任务开始时按条件启动 Perfetto ring buffer
- `CRITICAL` 卡顿事件触发导出 trace
- 导出结果写入：
  - `trace_artifacts`
  - `jank_events[].trace_path`

验收标准：

- 设备支持时可成功导出 trace 文件
- 导出失败不会导致 Fastbot 任务失败
- 单任务导出次数受限


### Task 7：实现 Perfetto trace 结构化分析

目标：

- 让 trace 文件能变成可读的分析结果，而不只是附件

涉及文件：

- 可新增：
  - `backend/jank_analyzer.py`
  - `backend/tests/`

改动内容：

- 读取导出的 trace
- 在指定时间窗内执行 SQL 分析
- 输出结构化结论，例如：
  - Top jank frames
  - Top busy threads
  - Main thread / RenderThread 耗时摘要
  - 可疑技术栈标记

验收标准：

- 输入 trace 后可得到 JSON 结构化结果
- 失败时有明确错误状态，不影响主报告浏览


### Task 8：接入 AI 卡顿根因总结

目标：

- 把结构化分析结果转成自然语言结论

涉及文件：

- `backend/api/log_analysis.py` 或独立新接口
- 可新增：
  - `backend/jank_ai_service.py`

改动内容：

- 输入结构化卡顿分析结果
- 输出：
  - 现象摘要
  - 最可能原因
  - 次要怀疑点
  - 排查建议

验收标准：

- 没有结构化结果时不调用 AI
- AI 输出失败不影响报告主链路


## 17. 推荐开发顺序

建议严格按下面顺序推进：

1. `Task 1`
   先把数据模型和接口打通，否则后面监控数据没地方落。

2. `Task 2`
   先做 `gfxinfo` 轻量巡检，最快拿到第一版趋势图。

3. `Task 3`
   再做状态机与去重，控制噪音。

4. `Task 4`
   补齐 summary，让报告页渲染简单稳定。

5. `Task 5`
   报告页展示闭环完成，此时已经可以交付一版可用功能。

6. `Task 6`
   加 Perfetto 证据保全，把“发现问题”升级成“保留现场”。

7. `Task 7`
   做结构化分析，让 trace 可读。

8. `Task 8`
   最后接 AI 总结，提升诊断体验。


## 18. 建议拆成的里程碑

### Milestone 1：卡顿趋势可视化

包含：

- Task 1
- Task 2
- Task 4
- Task 5

交付结果：

- 报告页能看到 FPS / 卡顿率趋势和卡顿事件

### Milestone 2：严重卡顿证据保全

包含：

- Task 3
- Task 6

交付结果：

- 严重卡顿发生时可自动导出 trace

### Milestone 3：根因分析增强

包含：

- Task 7
- Task 8

交付结果：

- trace 可转为结构化诊断和 AI 中文结论


## 19. 建议优先实现的最小版本

如果希望最快上线，建议先只做下面这些：

- 数据库新增 `jank_data` / `jank_events`
- 后端实现 `_monitor_jank()`
- 报告页增加：
  - FPS 曲线
  - 卡顿率曲线
  - 卡顿事件表

先不做：

- Perfetto
- trace 导出
- SQL 分析
- AI 诊断

这样第一版投入最小，但已经能回答最关键的问题：

- 本次探索有没有明显卡顿
- 卡顿发生在什么时候
- 卡顿严重程度如何


## 20. 可以直接创建的开发子任务

如果按工程任务分派，建议直接拆成下面这些 issue：

- `feat(fastbot): add jank report fields and migrations`
- `feat(fastbot): implement gfxinfo-based jank monitor`
- `feat(fastbot): add jank threshold state machine and dedup`
- `feat(fastbot): extend report summary with jank metrics`
- `feat(frontend): render jank charts and event table in fastbot report`
- `feat(fastbot): support perfetto ring buffer export on critical jank`
- `feat(fastbot): analyze perfetto trace for jank root cause`
- `feat(ai): summarize structured jank analysis into diagnosis report`


## 21. 后续优化待办

- 将“报告详情页触发懒分析”改造成独立后台任务或显式重分析接口，避免 `GET /reports/{task_id}` 带副作用。
- 为 `trace_artifacts[].ai_summary` 补充元数据：
  - `ai_summary_model`
  - `ai_summary_generated_at`
  - `ai_summary_error`
- 提供历史 trace 批量回填脚本，统一补齐结构化分析与 AI 总结。
- 强化报告页联动，在卡顿事件表里直接展示对应 trace 的首要怀疑点与跳转入口。
- 为 AI 调用补充治理能力：
  - 调用频控
  - 超时/重试策略
  - 审计日志
  - 模型切换后的缓存失效策略
