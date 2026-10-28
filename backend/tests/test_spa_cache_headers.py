"""SPA 托管缓存策略回归：index.html 永不缓存（部署换版后刷新必须拿到新壳）。"""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import spa


class SpaCacheHeaderTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(spa.router)
        self.client = TestClient(app)

    def test_index_html_is_not_cached(self):
        resp = self.client.get("/")
        if resp.status_code == 503:
            self.skipTest("前端产物未构建")
        self.assertEqual(resp.headers.get("cache-control"), "no-cache")

    def test_spa_fallback_index_is_not_cached(self):
        resp = self.client.get("/execution/reports/66")
        if resp.status_code == 503:
            self.skipTest("前端产物未构建")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-cache")

    def test_hashed_assets_are_immutable(self):
        resp = self.client.get("/favicon.ico")
        if resp.status_code == 503:
            self.skipTest("前端产物未构建")
        if resp.status_code == 200:
            self.assertIn("immutable", resp.headers.get("cache-control", ""))


if __name__ == "__main__":
    unittest.main()
