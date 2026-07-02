"""
OCR 引擎全局单例服务

解决多进程/多线程场景下 OCR 引擎重复初始化的问题：
- 进程级单例：每个进程只初始化一次
- 延迟加载：首次使用时才加载模型
- 线程安全：使用锁保护初始化过程
- 预热机制：可选的后台预热，减少首次调用延迟
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OCRService:
    """OCR 引擎全局单例服务"""

    _engine: Optional[Any] = None
    _lock = threading.Lock()
    _prewarm_started = False
    _prewarm_lock = threading.Lock()
    _initialization_time: Optional[float] = None

    @classmethod
    def get_engine(cls, use_angle_cls: bool = False, lang: str = "ch") -> Any:
        """
        获取 OCR 引擎实例（懒加载 + 线程安全）

        Args:
            use_angle_cls: 是否使用角度分类
            lang: 语言类型

        Returns:
            PaddleOCR 引擎实例

        Raises:
            RuntimeError: OCR 引擎初始化失败
        """
        if cls._engine is None:
            with cls._lock:
                # 双重检查锁定
                if cls._engine is None:
                    start_time = time.time()
                    logger.info("初始化 OCR 引擎（use_angle_cls=%s, lang=%s）...", use_angle_cls, lang)

                    try:
                        from backend.utils.ocr_compat import create_paddle_ocr_engine
                        cls._engine = create_paddle_ocr_engine(
                            use_angle_cls=use_angle_cls,
                            lang=lang
                        )
                        cls._initialization_time = time.time() - start_time
                        logger.info(
                            "OCR 引擎初始化完成，耗时 %.2f 秒",
                            cls._initialization_time
                        )
                    except Exception as exc:
                        logger.error("OCR 引擎初始化失败: %s", exc, exc_info=True)
                        raise RuntimeError(
                            "OCR 引擎初始化失败，请确保已安装 paddleocr 及其依赖"
                        ) from exc

        return cls._engine

    @classmethod
    def is_initialized(cls) -> bool:
        """检查 OCR 引擎是否已初始化"""
        return cls._engine is not None

    @classmethod
    def get_initialization_time(cls) -> Optional[float]:
        """获取初始化耗时（秒）"""
        return cls._initialization_time

    @classmethod
    def start_prewarm(cls, use_angle_cls: bool = False, lang: str = "ch") -> None:
        """
        启动后台预热线程

        预热包括：
        1. 初始化 OCR 引擎
        2. 使用空白图像执行一次识别（预热模型）

        Args:
            use_angle_cls: 是否使用角度分类
            lang: 语言类型
        """
        with cls._prewarm_lock:
            if cls._prewarm_started:
                logger.debug("OCR 预热已启动，跳过")
                return
            cls._prewarm_started = True

        thread = threading.Thread(
            target=cls._prewarm_worker,
            name="ocr-prewarm",
            daemon=True,
            kwargs={"use_angle_cls": use_angle_cls, "lang": lang}
        )
        thread.start()
        logger.info("OCR 预热线程已启动")

    @classmethod
    def _prewarm_worker(cls, use_angle_cls: bool = False, lang: str = "ch") -> None:
        """预热工作线程"""
        try:
            # 初始化引擎
            engine = cls.get_engine(use_angle_cls=use_angle_cls, lang=lang)

            # 使用空白图像预热模型
            try:
                import numpy as np
                from backend.utils.ocr_compat import run_paddle_ocr

                blank_image = np.zeros((48, 192, 3), dtype=np.uint8)
                run_paddle_ocr(engine, blank_image, use_cls=use_angle_cls)
                logger.info("OCR 引擎预热完成")
            except Exception as exc:
                logger.warning("OCR 引擎预热失败（不影响后续使用）: %s", exc)

        except Exception as exc:
            logger.error("OCR 预热线程异常: %s", exc, exc_info=True)

    @classmethod
    def reset(cls) -> None:
        """
        重置引擎状态（主要用于测试）

        注意: 不会释放已加载的模型内存，仅清除引用
        """
        with cls._lock:
            cls._engine = None
            cls._initialization_time = None
        with cls._prewarm_lock:
            cls._prewarm_started = False
        logger.info("OCR 引擎状态已重置")


# 全局便捷函数
def get_ocr_engine(use_angle_cls: bool = False, lang: str = "ch") -> Any:
    """获取 OCR 引擎实例（全局单例）"""
    return OCRService.get_engine(use_angle_cls=use_angle_cls, lang=lang)


def start_ocr_prewarm(use_angle_cls: bool = False, lang: str = "ch") -> None:
    """启动 OCR 引擎后台预热"""
    OCRService.start_prewarm(use_angle_cls=use_angle_cls, lang=lang)
