"""pytest 全局夹具。

模板图安全隔离：任何走用例创建/更新/删除链路的测试都会触发
`_cleanup_unused_template_images`。若不隔离，测试会拿测试库当引用参照
去扫真实的 static/images——历史上这批量误删过生产模板图（用例图片经常
丢失的根因）。这里对全部用例统一把 cases 模块的 PROJECT_ROOT /
AUTO_TEMPLATE_IMAGE_DIR 重定向到临时目录；个别测试（如
test_case_image_cleanup）自带 patch 的照旧覆盖，嵌套还原安全。
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_template_image_dir():
    from backend.api import cases as case_api

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        images = root / "static" / "images"
        images.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(case_api, "PROJECT_ROOT", root),
            patch.object(case_api, "AUTO_TEMPLATE_IMAGE_DIR", images),
        ):
            yield
