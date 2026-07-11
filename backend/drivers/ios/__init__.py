"""
IOSDriver 组成 Mixin 包

按内聚性拆分的 IOSDriver 能力模块（均为 Mixin，不可独立实例化）：
- support: 日志与诊断辅助
- locator: 定位与候选回退、弹窗点击链路
- vision: 截图 / OCR / 图像匹配
- app_control: 应用控制、back/home 与页面签名

对外入口保持 `from backend.drivers.ios_driver import IOSDriver` 不变。
"""
from .app_control import IOSAppControlMixin
from .locator import IOSLocatorMixin
from .support import IOSDriverSupportMixin
from .vision import IOSVisionMixin

__all__ = [
    "IOSAppControlMixin",
    "IOSDriverSupportMixin",
    "IOSLocatorMixin",
    "IOSVisionMixin",
]
