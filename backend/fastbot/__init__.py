"""
Fastbot 智能探索执行引擎（模块化拆分）

模块职责：
- adb          : ADB 协程工具（shell/push/pull/进程终止等）
- reporting    : Fastbot 报告目录管理
- perfetto     : Perfetto trace 会话、导出与分析
- perf_monitor : CPU/内存采集与 gfxinfo 汇总解析
- framestats   : 逐帧数据解析、卡顿分级与卡顿监控协程
- logcat       : 崩溃/ANR logcat 监控
- monkey       : Fastbot 资源部署与 Monkey 命令拼接
- startup      : 冷热启动专项测试
- summary      : 报告汇总与卡顿结论
- runner       : 顶层任务编排（run_fastbot_task / run_manual_fluency_session）

兼容入口：backend.fastbot_runner 以 re-export 方式保留旧导入路径。
"""
