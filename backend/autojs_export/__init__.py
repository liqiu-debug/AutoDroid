"""AutoDroid 脚本导出。

模块划分：
- generator: 标准步骤 -> 脚本步骤编译（纯数据转换）
- precheck:  可导出性预检
- packager:  脚本包（zip）组装
- runtime.js: 设备端运行时库（随包分发，解释执行脚本步骤）
"""
