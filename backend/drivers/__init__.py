"""
AutoDroid 跨端驱动模块

提供 Android/iOS 双端统一驱动接口，支持策略模式和工厂模式。
"""
from .base_driver import BaseDriver
from .android_driver import AndroidDriver
from .ios_driver import IOSDriver
from .cross_platform_runner import TestCaseRunner, DriverFactory

__all__ = [
    "BaseDriver",
    "AndroidDriver",
    "IOSDriver",
    "TestCaseRunner",
    "DriverFactory",
]
