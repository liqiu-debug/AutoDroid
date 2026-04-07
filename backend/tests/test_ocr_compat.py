import unittest

from backend.utils.ocr_compat import extract_ocr_text, iter_ocr_text_items, run_paddle_ocr


class _LegacyEngine:
    def __init__(self):
        self.calls = []

    def ocr(self, image, cls=True):
        self.calls.append(("ocr", cls))
        return [
            [
                [[[10, 10], [20, 10], [20, 20], [10, 20]], ["确定", 0.95]],
                [[[30, 30], [40, 30], [40, 40], [30, 40]], ["99.00", 0.88]],
            ]
        ]


class _PredictFallbackEngine:
    def ocr(self, image, cls=True):
        raise TypeError("PaddleOCR.predict() got an unexpected keyword argument 'cls'")

    def predict(self, image, cls=False):
        return [
            {
                "dt_polys": [
                    [[10, 10], [20, 10], [20, 20], [10, 20]],
                    [[30, 30], [40, 30], [40, 40], [30, 40]],
                ],
                "rec_texts": ["确定", "99.00"],
                "rec_scores": [0.91, 0.85],
            }
        ]


class _PredictWithoutClsEngine:
    def ocr(self, image, cls=True):
        raise TypeError("PaddleOCR.predict() got an unexpected keyword argument 'cls'")

    def predict(self, image, **kwargs):
        if "cls" in kwargs:
            raise TypeError("predict() got an unexpected keyword argument 'cls'")
        return {"result": [{"text": "确定", "score": 0.92, "box": [[1, 1], [2, 1], [2, 2], [1, 2]]}]}


class OCRCompatTests(unittest.TestCase):
    def test_run_paddle_ocr_with_legacy_ocr(self):
        engine = _LegacyEngine()
        result = run_paddle_ocr(engine, image="dummy", use_cls=False)
        self.assertEqual(engine.calls, [("ocr", False)])
        text = extract_ocr_text(result)
        self.assertIn("确定", text)
        self.assertIn("99.00", text)

    def test_run_paddle_ocr_fallback_to_predict(self):
        engine = _PredictFallbackEngine()
        result = run_paddle_ocr(engine, image="dummy", use_cls=False)
        items = iter_ocr_text_items(result)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["text"], "确定")
        self.assertIsNotNone(items[0]["box"])

    def test_run_paddle_ocr_predict_without_cls_kwarg(self):
        engine = _PredictWithoutClsEngine()
        result = run_paddle_ocr(engine, image="dummy", use_cls=False)
        text = extract_ocr_text(result)
        self.assertEqual(text, "确定")


if __name__ == "__main__":
    unittest.main()
