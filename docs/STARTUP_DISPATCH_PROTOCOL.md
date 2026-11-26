# 冷热启动派发与恢复协议

本协议对应 `startup-v2` 测量口径、桌面启动协议版本 1。Android 和 iOS 的采集器、统计与六列报告形状来自 `backend/startup/`，云端仍使用原有 `FastbotTask`、`FastbotReport`，不为派发另建一份任务或报告。

## 部署顺序

1. 先备份数据库及 `reports/`，更新平台。启动时迁移 `20261125_042_startup_execution` 创建 `ClientInstance` 与 `StartupExecution`，保留旧 Android 请求及历史报告读取。
2. 更新桌面引擎及共享前端。桌面设备登记、心跳和 claim 上报启动能力，包含协议版本、平台、指标、就绪 locator 与 Web 派发能力；旧客户端设备会显示升级原因，平台不会把这些设备交给服务器 ADB。
3. 先验证桌面 Android，再验证 macOS 上已有健康 WDA 的 iOS，最后验证 Web 到在线桌面的派发。无真机环境的自动化测试不代替真机验收；Windows 不提供首次构建 WDA 的能力。

服务端 Android 原轮次 Trace 分析使用独立的 `backend/startup/trace_analysis.py`，资源仅来自本仓库，不依赖桌面源码。macOS/Windows 构建部署时运行 `python scripts/fetch-trace-processor.py --platform mac-arm64`（或 `mac-x64`、`windows-x64`），随后执行相同命令加 `--verify-only` 验证固定 v58.2 的大小与 SHA256，并将整个 `assets/trace_processor/` 随部署包发布；该目录为生成资源，不提交 Git。官方来源、提交和 Apache 2.0 许可证保留在 `scripts/trace-processor-manifest.json` 与 `scripts/trace-processor-LICENSE.txt`。运行时只执行校验过的本地二进制，不下载工具。

当前固定清单尚未提供 Linux 目标。Linux、缺工具、校验失败、Trace 缺失都在报告中明确显示 `UNAVAILABLE` 和具体原因，保留原始 Trace，任务不会永久留在 `PENDING`。可用的结构化结果为 `ANALYZED/PARTIAL`，`analysis_level=startup_evidence`；主线程和调度信息属于区间内证据，不自动宣称为根因或系统首帧指标。原有流畅度 Trace AI 不接收这类启动证据。

当前平台运行时仍要求一个服务进程。FIFO 执行限流、设备驱动与执行线程不支持多 worker 跨进程共享。

## 对外接口

| 接口 | 行为 |
| --- | --- |
| `GET /api/fastbot/startup/devices` | 返回 `items`；每设备含 `platform`、`executor_kind`、`client_instance_id`、`available`、`unavailable_reason`、`capabilities` |
| `POST /api/fastbot/startup/run` | 按设备数据库归属选择服务端或桌面节点；同用户同 `request_id` 和配置返回同任务；同键不同配置返回 409 |
| `GET /api/fastbot/startup/tasks` | 当前账号任务分页，含进度、取消状态、配置及报告摘要；管理员可见全部 |
| `POST /api/fastbot/startup/tasks/{id}/stop` | 排队任务直接取消；运行任务通知原执行者停止，不提前释放尚在执行的设备 |
| `GET /api/fastbot/tasks/{id}`、`GET /api/fastbot/reports/{id}` | 读取同一任务与报告，测量中也可读取已完成轮次 |

一次请求只允许同平台设备。提交事务对完整批次原子登记设备占用，任何归属/忙碌/版本校验失败都会回滚。设备专属预检在执行者拿到占用后运行，锁屏、未安装、WDA/ADB 错误分别保留到该设备的失败报告，不统一伪装成离线。

## 桌面认领与结果上传

- 本地发起仍使用 `POST /api/client-fastbot`，`kind=startup` 携带 `startup_config`、`client_instance_id`、稳定 `run_id`，直接返回同任务的 `attempt`、`claim_token` 和带时区的 `lease_expires_at`。
- Web 派发由 `POST /api/client-fastbot/startup/claim` 认领。请求包含实例、能力及当前本地 `active_run_ids`，不会因同实例另一台设备在执行而阻塞后续设备命令。
- `POST /api/client-fastbot/startup/{id}/progress` 接受进度与完整六列 `partial_report`。进度字段为 `completed_count/requested_count/valid_count/invalid_count/phase/current_mode/current_iteration`。上传失败不能丢弃本地采样结果。
- `POST /api/client-fastbot/startup/{id}/finish` 接受 `COMPLETED/FAILED/ABORTED` 和报告。重复完成不会重写已完成的终态。所有修改均校验账号、实例、`attempt` 与 `claim_token`。
- 原产物上传接口增加同一 guard，服务端将路径写入 `attempts/{attempt}/`，返回的 canonical path 必须回填报告。路径穿越和超过 100 MiB 的单个产物会被拒绝。
- `POST /api/client-fastbot/startup/{id}/recover` 只恢复上传，永远不启动采样。重登后同账号、实例、`run_id` 和 attempt 可重新取得上传 guard，无需把登录凭据或执行 token 存入本地待传文件。已经完整采集但未同步的结果携带 `local_status=COMPLETED/FAILED`；实际进程中断则使用 `ABORTED`。

采样结束后的本地 trace 分析与产物上传仍需续租，直到 `finish` 返回；设备调用完成后可释放本机资源，但云端预约在终态确认前保持。云端仅对终态启动报告做补充 trace 分析，进行中的 AI trace 分析返回 409。耗时分析在写回前刷新最新报告并校验快照，保留分析期间补传的报告摘要与产物。

服务端 worker 从构造设备适配器之前开始每 5 秒独立续租，仅更新租约与取消控制，不覆盖报告；原轮次采集与设备清理结束后执行启动专用离线分析，再保存同一任务的终态报告。续租覆盖预检、采样、Trace 分析及完成写回，避免慢 WDA 初始化或离线分析误触发 90 秒失联判断。

## 失联、重启与设备互斥

运行租约为 90 秒，桌面每次采样动作前检查本地租约截止，并以有限超时的 ADB/WDA 调用执行。租约过期不能由迟到进度复活：云端将任务收敛为 `ABORTED`，保留已完成轮次与原因。旧 attempt 不会自动交给另一节点重新测量。

云端收敛失联桌面任务后，设备占用继续保留 **120 秒安全宽限**，让在途设备调用与清理结束。同 attempt 的安全完成或重启恢复确认可以提前释放；否则清理器在宽限到期后只释放仍匹配 `startup:{task_id}` 的设备 owner，绝不清掉其他新任务的占用。报告保留 `interruption_reason`、`measurement_resumed=false` 和宽限后释放标记。失联节点必须重新上报心跳及设备能力才会再次可选。

服务端重启使其在飞采集进入 `ABORTED`，不自动续跑计时样本。排队命令持久化并有 30 分钟截止。取消运行中的服务端采集只设置停止事件，核心完成调用和清理后才释放租约。桌面退出、登录切换、设备换归属会停止旧执行并保留其账号范围内的待传结果。

同一桌面的固定实例 UUID 在明确登出后，或通过本机物理设备重新登记时可以切换到新账号；单纯 heartbeat/claim 不接管仍登录的其他账号实例。切换时旧排队任务立即取消，旧运行任务收敛为 `ABORTED` 并保留安全宽限预约，旧 attempt 只能由原账号补传证据，不能复活或被新账号接手采样。

启动采样与用例、场景、Fastbot 共用设备排他控制。录制、单步、层级与设备交互在启动占用期间拒绝介入；iOS 在采集前安全排空旧录制会话，避免录制池超时清理关闭采样会话。历史报告删除和保留策略也会清理对应命令，但不会删除仍等待节点停止确认的记录。
