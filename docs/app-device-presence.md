# App 在线状态（2026-10-20）

iOS 和 Android 登录后以各自的 App 设备标识调用 `POST /api/device-runs/register`，携带 `executor_status`。前台约 60 秒一次，状态变化提前更新；Android 有脚本引擎运行时也尝试维持心跳。没有为在线展示额外创建后台常驻服务。`POST /api/device-runs/logout` 主动下线；断网无法发送时由 3 分钟超时兜底。

Web 设备中心每 5 秒更新列表，App 在卡片顶部仅显示“App 在线”或“App 离线”；心跳超时和退出登录统一显示离线。USB、远程 USB、WDA 无线和 Android ADB 网络保持独立标签。卡片不展示执行器、最近心跳及解释信息；同机绑定入口收进“更多”。后台仍保存这些状态，心跳超时不能据此终止执行或判断任务失败。

App 的 IDFV / Android ID 不等于 USB UDID / ADB serial，因此不自动猜测同机关系。点击“绑定有线／无线设备”选择同平台的真实设备，绑定后合并展示但保留数据库中的两条记录；可随时解绑。只有登记 App 的账号可绑定、解绑或主动下线，目标设备不能重复绑定；删除已绑定物理设备前必须解绑。App-only 设备隐藏物理控制入口，后端也拒绝将 App 标识传给主机控制接口。

数据库迁移仅增加 App 最近心跳、退出时间、执行器状态、登记账号、绑定设备字段及唯一索引，兼容原有 `autojs6` / `wda` 接入类型。心跳刷新不会修改物理设备状态，也不会延长或回收任务租约。

## 验证

- 后端 57 项通过（新增 9 项覆盖双平台心跳、超时、退出/重登、绑定/解绑、权限/唯一约束、主机操作隔离、同步计数、迁移），命令：`.venv/bin/python -m pytest backend/tests/test_app_device_presence.py backend/tests/test_device_list_wda_status.py backend/tests/test_autojs_export.py -q`。
- Swift 客户端 20 项通过，包括退出接口认证、身份、超时和请求体；`swift test`。
- Web `npm run build` 通过。真实 DeviceCenter 组件使用隔离数据检查桌面绑定、移动端解绑、App-only 控制按钮隐藏、USB 离线且 App 在线同时展示。未绑定或修改用户真实设备记录。
- iOS 0.2.2 (4) 真机构建并安装到连接的 iPhone；本轮未操作用户账号退出登录。
- Android `assembleAppDebug --offline` 构建通过，6.7.0 (3818)。没有连接 Android 真机，本轮未验证 Android 真机后台调度和退出流程。安装包在相邻 AutoJs6 项目的 `app/build/outputs/apk/app/debug/`。

后台挂起、省电或断网都可能导致 App 心跳超时。在线仅代表最近收到 App 心跳，不等同于 Web 可远程控制，也不保证后台定时任务一定能执行。
