"""
SPA 前端托管

从 backend.main 拆出的前端静态托管：
- `/` 返回 index.html
- `/{full_path:path}` 兜底路由：静态资源直出、前端路由回退 index.html
- 前端构建产物缺失时返回 503 提示

注意：`/{full_path:path}` 是全局兜底路由，必须在所有 API 路由之后挂载。
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from backend.paths import PROJECT_ROOT

FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"
FRONTEND_INDEX_FILE = FRONTEND_DIST_DIR / "index.html"
SPA_EXCLUDED_PREFIXES = (
    "api",
    "auth",
    "cases",
    "folders",
    "scenario-folders",
    "scenarios",
    "reports",
    "tasks",
    "fastbot",
    "devices",
    "device",
    "run",
    "ws",
    "docs",
    "redoc",
    "openapi.json",
    "static",
    "report-assets",
)

router = APIRouter()


def _frontend_build_missing_response():
    return PlainTextResponse(
        "前端构建产物不存在。请先执行 `cd frontend && npm run build`，或直接运行 `./scripts/start_lan.sh`。",
        status_code=503,
    )


@router.get("/", include_in_schema=False)
async def serve_frontend_index():
    if FRONTEND_INDEX_FILE.is_file():
        return FileResponse(str(FRONTEND_INDEX_FILE))
    return _frontend_build_missing_response()


@router.get("/{full_path:path}", include_in_schema=False)
async def serve_frontend_app(full_path: str):
    normalized_path = str(full_path or "").strip("/")
    if not normalized_path:
        return await serve_frontend_index()

    if any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in SPA_EXCLUDED_PREFIXES
    ):
        raise HTTPException(status_code=404, detail="Not found")

    if FRONTEND_DIST_DIR.is_dir():
        dist_root = FRONTEND_DIST_DIR.resolve()
        candidate = (dist_root / normalized_path).resolve()
        try:
            candidate.relative_to(dist_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found")

        if candidate.is_file():
            return FileResponse(str(candidate))

        if Path(normalized_path).suffix:
            raise HTTPException(status_code=404, detail="Not found")

    if FRONTEND_INDEX_FILE.is_file():
        return FileResponse(str(FRONTEND_INDEX_FILE))

    return _frontend_build_missing_response()
