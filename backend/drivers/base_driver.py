"""
BaseDriver — 跨端驱动抽象基类

所有端侧驱动（Android / iOS）必须实现此接口。
采用策略模式，确保上层调度器（TestCaseRunner）与具体自动化库解耦。
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class BaseDriver(ABC):
    """
    设备驱动抽象基类。

    子类需实现以下四个核心方法，上层 Runner 通过统一接口分发动作，
    无需关心底层使用的是 uiautomator2 还是 facebook-wda。
    """

    def __init__(self, device_id: str) -> None:
        """
        初始化驱动并连接设备。

        Args:
            device_id: 设备唯一标识（Android serial 或 iOS UDID）。
        """
        self.device_id = device_id
        logger.info("[%s] 初始化驱动: device_id=%s", self.__class__.__name__, device_id)

    # ------------------------------------------------------------------ #
    #  核心抽象方法 — 子类必须实现
    # ------------------------------------------------------------------ #

    @abstractmethod
    def click(self, selector: str, by: str) -> None:
        """
        点击元素。

        Args:
            selector: 定位值（如 resourceId、label、xpath 等）。
            by: 定位策略（如 "id", "text", "xpath", "label", "name"）。
        """
        ...

    @abstractmethod
    def input(self, selector: str, by: str, text: str) -> None:
        """
        向元素输入文本。

        Args:
            selector: 定位值。
            by: 定位策略。
            text: 要输入的文本内容。
        """
        ...

    @abstractmethod
    def screenshot(self) -> bytes:
        """
        截取当前屏幕。

        Returns:
            PNG 格式的截图字节流。
        """
        ...

    @abstractmethod
    def click_by_coordinates(self, x: float, y: float) -> None:
        """
        按物理坐标点击屏幕。

        Args:
            x: 物理像素 X 坐标。
            y: 物理像素 Y 坐标。
        """
        ...

    # ------------------------------------------------------------------ #
    #  扩展能力（跨端 Runner 会按动作调用）
    # ------------------------------------------------------------------ #

    def wait_until_exists(self, selector: str, by: str, timeout: int = 10) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 wait_until_exists")

    def assert_text(
        self,
        selector: str = "",
        by: str = "",
        expected_text: str = "",
        match_mode: str = "contains",
    ) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 assert_text")

    def input_focused(self, text: str) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 input_focused")

    def swipe(self, direction: str) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 swipe")

    def back(self) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 back")

    def home(self) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 home")

    def start_app(self, app_id: str) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 start_app")

    def stop_app(self, app_id: str) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 stop_app")

    def click_image(self, image_path: str) -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 click_image")

    def assert_image(self, image_path: str, match_mode: str = "exists") -> None:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 assert_image")

    def extract_by_ocr(self, region: str, extract_rule: Optional[Dict[str, Any]] = None) -> str:
        raise NotImplementedError(f"{self.__class__.__name__} 不支持 extract_by_ocr")

    # ------------------------------------------------------------------ #
    #  通用辅助方法（子类可选重写）
    # ------------------------------------------------------------------ #

    def disconnect(self) -> None:
        """断开设备连接（默认空实现，子类按需重写）。"""
        logger.info("[%s] 断开设备连接: %s", self.__class__.__name__, self.device_id)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} device_id={self.device_id!r}>"
