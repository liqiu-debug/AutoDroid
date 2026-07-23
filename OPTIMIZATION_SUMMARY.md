# AutoDroid 历史优化实施总结

> 原始优化批次完成于 2026-07-02；本文于 2026-07-23 按当前代码基线复核。
> 本文保留早期性能治理的背景和结论，不作为当前 API、回滚命令或架构边界的唯一依据。

## 1. 原始优化范围

首批优化处理了三个高优先级问题：OCR 重复初始化、用例列表 N+1 查询，以及缺少执行并发控制。

### 1.1 OCR 引擎单例

问题：Android、iOS 和不同执行入口重复创建 PaddleOCR，首次加载慢且内存开销大。

落地：

- `backend/ocr_service.py` 提供进程内、线程安全的延迟单例。
- Android/iOS 驱动通过统一服务获取 OCR 引擎。
- 支持后台预热，首次真实 OCR 步骤无需再重复构造模型。
- PaddleOCR 仍是可选能力；未安装时由兼容层给出可诊断失败，不影响不使用 OCR 的流程。

预期收益：同一后端进程只维护一组相同配置的 OCR 引擎，后续获取接近常数开销。多进程部署仍会按进程各自加载模型。

### 1.2 用例列表 N+1 查询

问题：列表中的每个用例曾单独查询一次目录，100 条记录可能产生 `1 + N` 次数据库查询。

落地：`backend/api/cases.py` 在主查询中 `LEFT JOIN` 目录信息，并把预加载的目录名交给响应组装逻辑。

预期收益：列表主数据和目录信息在一次查询中返回，数据库连接占用和大列表延迟显著下降。具体耗时受数据库规模、磁盘和并发影响，不再承诺固定倍数。

### 1.3 执行并发治理

问题：缺少全局、用户和设备维度的并发约束，多个任务可能争用同一设备。

最初版本引入 `backend/execution_limiter.py` 和 `/api/limiter/*` 状态接口。该实现此后已经演进，当前语义如下：

- `AUTODROID_LIMIT_GLOBAL` 和 `AUTODROID_LIMIT_PER_USER` 控制全局/用户上限，默认 20/5。
- 超限的用例、场景和定时任务进入 FIFO 队列，不再把正常排队统一返回为 HTTP 429。
- `AUTODROID_QUEUE_TIMEOUT` 控制最长排队时间，默认 1800 秒。
- 巡检和兼容性等长任务使用 `backend/device_execution_lease.py`，同时持有内存限流租约与数据库 owner 租约。
- 设备释放会校验 `lease_task_id`，旧任务不能误把新任务占用的设备恢复为 `IDLE`。

## 2. 从首批优化到当前基线

| 领域 | 首批状态 | 当前状态 |
|---|---|---|
| OCR | 驱动共享进程内单例 | 保留单例与兼容层，按需安装 PaddleOCR |
| 用例查询 | 目录 JOIN 消除 N+1 | 保留 JOIN，SQLite 已启用 WAL、NORMAL synchronous 和 30s busy timeout |
| 并发控制 | 固定上限、直接拒绝 | 环境变量配置、FIFO 排队、可取消、排队超时、owner-safe 设备租约 |
| 执行器 | legacy 与跨端 Runner 并存 | 仅保留标准跨端 Runner，legacy `runner.py` 已删除 |
| 步骤数据 | legacy JSON 为主 | `TestCaseStep` 标准表默认开启，legacy 数据幂等回填并保留兼容读取 |
| 报告磁盘 | 手动清理 | 每日保留任务覆盖 UI/Fastbot/兼容性/巡检；默认按天清理关闭 |
| 证据资产 | 报告目录文件 | 可灰度双写内容寻址资产，支持引用、鉴权读取、分层保留和容量水位 |
| 工程验证 | 手工验证 | GitHub Actions 执行 Ruff、1087 个后端测试和前端构建；另有 72 个前端 Node 测试 |

## 3. 当前配置

### 3.1 环境变量

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `AUTODROID_LIMIT_GLOBAL` | `20` | 全局并发上限 |
| `AUTODROID_LIMIT_PER_USER` | `5` | 单用户并发上限 |
| `AUTODROID_QUEUE_TIMEOUT` | `1800` | FIFO 排队超时秒数 |
| `AUTODROID_DRIVER_POOL` | `0` | 是否复用设备驱动连接 |

### 3.2 SystemSetting

- `report_retention_days`：大于 0 时按天删除已结束的过期报告；0/缺失为关闭。
- `content_addressed_assets`：证据双写到 `asset_store/`，默认关闭。
- `tiered_asset_retention`：HOT/WARM/PINNED/COLD 分层保留，依赖内容寻址资产，默认关闭。
- `asset_storage_low_watermark_percent`、`asset_storage_high_watermark_percent`、`asset_storage_critical_watermark_percent`：默认 80/90/95，用于状态与分层 GC；新巡检和兼容性任务的硬保护当前使用内置 95%。

## 4. 当前验证方式

安装开发依赖后运行：

```bash
.venv/bin/ruff check backend scripts
.venv/bin/python -m unittest discover -s backend/tests -p 'test_*.py'
cd frontend
npm run build
node --test tests/*.test.mjs
```

查看限流与资产状态：

```bash
curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8000/api/limiter/stats

curl -H "Authorization: Bearer <token>" \
  http://127.0.0.1:8000/api/assets/status
```

性能数字必须在目标部署环境中复测。不要用历史文档中的固定“3-5 倍”或“0ms”替代基准测试。

## 5. 回滚原则

- 不要按旧文档逐文件删除模块；当前执行、迁移、路由和测试之间已有新的依赖关系。
- 功能灰度优先使用 `SystemSetting` 开关，不通过手工修改数据库表或删除资产目录回滚。
- 内容寻址资产回滚前，使用 `scripts/maintenance/backfill_artifacts.py --materialize-legacy` 重建缺失的 legacy 路径并验证读取。
- 数据库迁移前后都应备份 `database.db`、`reports/` 和 `asset_store/`；运行中的设备任务先取消并确认租约释放。
- 代码级回滚应回退完整提交，并重新运行全量后端测试、前端测试和构建。

## 6. 后续治理

- 将 `frontend/tests/*.test.mjs` 接入 CI，并补充组件挂载级测试。
- 拆分新的巡检巨型模块前先稳定 Graph v8、实时快照和回放契约。
- 在默认开启内容寻址资产和分层保留前完成容量压测、24 小时恢复窗口与 legacy 物化演练。
- 常用 OCR 的部署环境应固化 PaddleOCR 版本和模型缓存策略。

## 7. 相关文档

- [项目深度说明](docs/PROJECT_OVERVIEW_CN.md)
- [执行规范](docs/EXECUTION_SPEC.md)
- [巡检、回放与证据资产指南](docs/INSPECTION_REPLAY_ASSETS.md)
- [CI 集成指南](docs/CI_INTEGRATION.md)
- [改进路线图](docs/IMPROVEMENT_ROADMAP.md)
