"""
智能日志分析 API

提供：
- Android Crash/ANR 日志清洗降噪算法
- LLM 智能根因分析调用
- MD5 缓存避免重复分析
"""
import re
import hashlib
import logging
from typing import List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import SystemSetting
from backend.openai_compat import parse_chat_completion_payload
from backend.schemas import LogAnalysisRequest, LogAnalysisResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ==================== 内存缓存 ====================
# Key: md5(cleaned_log), Value: analysis result string
_analysis_cache: dict = {}

ANR_ANCHOR_PATTERN = re.compile(r'ANR in', re.IGNORECASE)
CRASH_ANCHOR_PATTERNS = [
    re.compile(r'FATAL EXCEPTION', re.IGNORECASE),
    re.compile(r'AndroidRuntime', re.IGNORECASE),
    re.compile(r'Process:.*Crashing', re.IGNORECASE),
]


def _get_setting(session: Session, key: str, default: str = "") -> str:
    """从数据库读取系统配置"""
    setting = session.exec(
        select(SystemSetting).where(SystemSetting.key == key)
    ).first()
    return setting.value if setting else default


# ==================== 日志清洗算法 ====================

def clean_log_for_ai(full_log: str, package_name: str) -> str:
    """
    Android Crash/ANR 日志降噪算法。
    
    从 500 行原始日志中提取核心堆栈信息，目标压缩到 100 行以内。
    
    策略：
    1. 定位 FATAL EXCEPTION / AndroidRuntime / ANR in 所在行
    2. 从该行向下遍历，按优先级保留关键行
    3. 过滤掉无关系统噪音
    """
    lines = full_log.strip().split('\n')
    if not lines:
        return full_log

    anr_indices = [i for i, line in enumerate(lines) if ANR_ANCHOR_PATTERN.search(line)]
    if anr_indices:
        return _clean_anr_log_for_ai(lines, package_name, anr_indices)

    # Step 1: 定位关键起始行
    start_indices = []
    for i, line in enumerate(lines):
        for pattern in CRASH_ANCHOR_PATTERNS:
            if pattern.search(line):
                start_indices.append(i)
                break

    if not start_indices:
        # 没找到典型 Crash 标记，返回最后 80 行
        return '\n'.join(lines[-80:])

    # Step 2: 从每个锚点开始提取
    kept_lines = []
    processed_ranges = set()

    # 噪音模式 — 这些行通常无用
    noise_patterns = [
        re.compile(r'^\s*at java\.lang\.reflect\.'),
        re.compile(r'^\s*at com\.android\.internal\.'),
        re.compile(r'^\s*at android\.os\.Handler\.'),
        re.compile(r'^\s*at android\.os\.Looper\.'),
        re.compile(r'^\s*at com\.android\.server\.'),
        re.compile(r'^\s*at dalvik\.system\.'),
        re.compile(r'^\s*at libcore\.'),
        re.compile(r'^\s*at sun\.'),
        re.compile(r'^\s*at java\.util\.concurrent\.ThreadPoolExecutor'),
        re.compile(r'^\s*\.\.\. \d+ more'),
    ]

    for start_idx in start_indices:
        if start_idx in processed_ranges:
            continue

        system_stack_count = 0
        max_system_stack = 5  # 最多保留 5 行系统堆栈

        # 保留锚点行
        kept_lines.append(lines[start_idx])
        processed_ranges.add(start_idx)

        # 向下遍历
        for j in range(start_idx + 1, min(start_idx + 200, len(lines))):
            if j in processed_ranges:
                continue

            line = lines[j]
            stripped = line.strip()

            # 空行跳过
            if not stripped:
                continue

            # 遇到下一个 FATAL/AndroidRuntime 标记，停止当前块
            is_new_block = False
            for pattern in CRASH_ANCHOR_PATTERNS[:2]:
                if pattern.search(line) and j != start_idx:
                    is_new_block = True
                    break
            if is_new_block:
                break

            # 优先级 1: Caused by 行 — 必保留
            if 'Caused by:' in line or 'Caused by ' in line:
                kept_lines.append(line)
                processed_ranges.add(j)
                system_stack_count = 0  # 重置计数
                continue

            # 优先级 2: 包含用户包名的行 — 必保留
            if package_name and package_name in line:
                kept_lines.append(line)
                processed_ranges.add(j)
                continue

            # 优先级 3: 异常类名行 (如 java.lang.NullPointerException)
            if re.match(r'^\s*(java\.\w+\.\w*Exception|java\.\w+\.\w*Error)', stripped):
                kept_lines.append(line)
                processed_ranges.add(j)
                continue

            # 优先级 4: Process / PID 信息行
            if stripped.startswith('Process:') or stripped.startswith('PID:'):
                kept_lines.append(line)
                processed_ranges.add(j)
                continue

            # 噪音检测
            is_noise = False
            for noise_pat in noise_patterns:
                if noise_pat.search(line):
                    is_noise = True
                    break

            if is_noise:
                continue

            # 系统堆栈 (at android.app..., at android.view...) — 限量保留
            if re.match(r'^\s*at\s+(android\.|com\.android\.)', stripped):
                if system_stack_count < max_system_stack:
                    kept_lines.append(line)
                    processed_ranges.add(j)
                    system_stack_count += 1
                continue

            # 其他 at 行（非用户包、非系统核心）— 少量保留
            if re.match(r'^\s*at\s+', stripped):
                system_stack_count += 1
                if system_stack_count <= max_system_stack:
                    kept_lines.append(line)
                    processed_ranges.add(j)
                continue

            # 其他行（可能是异常消息等）保留
            kept_lines.append(line)
            processed_ranges.add(j)

    # 去重并限制行数
    result = []
    seen = set()
    for line in kept_lines:
        key = line.strip()
        if key not in seen:
            seen.add(key)
            result.append(line)

    # 如果清洗后太少，补充原始日志尾部
    if len(result) < 5:
        result = lines[-80:]

    # 限制上限
    if len(result) > 100:
        result = result[:100]

    return '\n'.join(result)


def _clean_anr_log_for_ai(lines: List[str], package_name: str, anr_indices: List[int]) -> str:
    """针对 ANR 日志走单独清洗，优先保留原因、CPU 概览和主线程栈。"""
    start_idx = anr_indices[-1]
    end_idx = min(start_idx + 260, len(lines))
    kept_lines: List[str] = []
    seen: Set[str] = set()

    pressure_lines = 0
    package_cpu_lines = 0
    logkit_stack_lines = 0
    plain_stack_lines = 0
    capture_plain_stack = False

    for j in range(start_idx, end_idx):
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            continue

        keep = False

        if any(
            marker in line for marker in (
                'ANR in',
                'PID:',
                'Reason:',
                'Parent:',
                'ErrorId:',
                'Frozen:',
                'Load:',
                'CPU usage from',
                ' TOTAL:',
                'DropBoxManagerService: file :: /data/system/dropbox/data_app_anr',
            )
        ):
            keep = True

        if package_name and package_name in line and any(
            marker in line for marker in ('ActivityManager:', '[Fastbot]:', 'Killing ', 'main thread')
        ):
            keep = True

        if '/proc/pressure/' in line and pressure_lines < 6:
            keep = True
            pressure_lines += 1

        if package_name and package_name in line and (
            'ActivityManager:' in line or '[Fastbot]:' in line
        ) and package_cpu_lines < 10:
            keep = True
            package_cpu_lines += 1

        if 'LogKit_AnrCrashDumpRunnable' in line and logkit_stack_lines < 60:
            keep = True
            logkit_stack_lines += 1
            if 'stackTrace=' in line or 'main thread' in line.lower():
                capture_plain_stack = True

        if re.match(r'^\s*at\s+', stripped):
            if capture_plain_stack and plain_stack_lines < 30:
                keep = True
                plain_stack_lines += 1
            elif not capture_plain_stack:
                continue
        elif capture_plain_stack and 'LogKit_AnrCrashDumpRunnable' not in line:
            capture_plain_stack = False

        if keep:
            key = stripped
            if key not in seen:
                seen.add(key)
                kept_lines.append(line)

    if len(kept_lines) < 5:
        fallback_start = max(0, start_idx - 5)
        return '\n'.join(lines[fallback_start:min(fallback_start + 100, len(lines))])

    return '\n'.join(kept_lines[:100])


# ==================== LLM 调用 ====================

def _build_system_prompt(package_name: str, device_info: str = "") -> str:
    """构造 System Prompt"""
    device_ctx = f"\n设备型号: {device_info}" if device_info else ""
    return f"""你是一位资深 Android 应用开发专家，精通 Crash 和 ANR 日志分析。
请分析以下清洗后的 Crash/ANR 日志。请忽略无关的系统信息，直接指出代码错误位置。
应用包名为: {package_name}{device_ctx}

请严格按照以下格式输出 Markdown:

### 🔴 错误摘要
简要概述崩溃类型和影响范围 (1-2 句话)。

### 🔍 根因分析
1. 指出异常类型 (如 NullPointerException)
2. 定位引发异常的具体代码位置 (类名 + 方法名 + 行号)
3. 分析可能的触发条件和调用链

### 🛠️ 修复建议
1. 给出具体的修复方案和代码示例
2. 如有必要，建议防御性编程措施
3. 推荐相关的测试用例"""


async def call_llm_service(
    cleaned_log: str,
    package_name: str,
    device_info: str,
    session: Session,
) -> dict:
    """
    调用 LLM API 进行日志分析。
    
    支持 OpenAI 兼容协议 (GPT / DeepSeek / 通义千问 等)。
    从数据库 SystemSetting 读取配置：
    - ai_api_key: API Key
    - ai_api_base: API Base URL (默认 https://api.openai.com/v1)
    - ai_model: 模型名 (默认 gpt-3.5-turbo)
    """
    import httpx

    api_key = _get_setting(session, "ai_api_key", "")
    api_base = _get_setting(session, "ai_api_base", "https://api.openai.com/v1")
    model = _get_setting(session, "ai_model", "gpt-3.5-turbo")

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="未配置 AI API Key。请在系统设置中配置 ai_api_key。"
        )

    # 去掉尾部斜杠，并自动修正常见的 URL 错误
    api_base = api_base.rstrip('/')
    # 如果用户填的 URL 末尾已经有 /chat/completions，去掉它
    if api_base.endswith('/chat/completions'):
        api_base = api_base[:-len('/chat/completions')]

    request_url = f"{api_base}/chat/completions"

    system_prompt = _build_system_prompt(package_name, device_info)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"请分析以下 Android Crash 日志:\n\n```\n{cleaned_log}\n```"},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    logger.info(f"LLM 请求 URL: {request_url}, 模型: {model}")

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                request_url,
                json=payload,
                headers=headers,
            )

            # 统一读取原始响应文本，供状态校验与错误诊断复用
            raw_text = resp.text
            logger.info(f"LLM 响应状态: {resp.status_code}, 长度: {len(raw_text)}")

            # 检查 HTTP 状态码
            if resp.status_code != 200:
                logger.error(f"LLM API 返回非200: {resp.status_code} - {raw_text[:500]}")
                raise HTTPException(
                    status_code=502,
                    detail=f"AI 服务返回错误 (HTTP {resp.status_code}): {raw_text[:300]}"
                )

            # 检查响应是否为空
            if not raw_text or not raw_text.strip():
                logger.error(f"LLM API 返回空响应, URL: {request_url}")
                raise HTTPException(
                    status_code=502,
                    detail=f"AI 服务返回空响应。请检查 API 地址是否正确: {request_url}"
                )

            # 尝试解析 JSON
            try:
                data = parse_chat_completion_payload(raw_text)
            except Exception as json_err:
                logger.error(
                    "LLM 响应解析失败: %s, 原始响应前200字符: %s",
                    json_err,
                    raw_text[:200],
                )
                raise HTTPException(
                    status_code=502,
                    detail=f"AI 服务返回无法解析的响应（非标准 JSON / SSE）。当前请求地址: {request_url}，响应内容: {raw_text[:150]}"
                )

            # 检查返回结构
            if "choices" not in data or not data["choices"]:
                logger.error(f"LLM 返回缺少 choices 字段: {data}")
                raise HTTPException(
                    status_code=502,
                    detail=f"AI 服务返回格式异常（缺少 choices）: {str(data)[:200]}"
                )

            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            total_tokens = usage.get("total_tokens", 0)

            return {
                "success": True,
                "analysis_result": content,
                "token_usage": total_tokens,
            }
    except HTTPException:
        raise  # 直接透传已处理的异常
    except httpx.TimeoutException:
        logger.error("LLM API 调用超时")
        raise HTTPException(status_code=504, detail="AI 服务响应超时，请稍后重试")
    except httpx.ConnectError as e:
        logger.error(f"LLM API 连接失败: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"无法连接 AI 服务。请检查 API 地址 ({api_base}) 是否可访问。"
        )
    except Exception as e:
        logger.error(f"LLM API 调用异常: {e}")
        raise HTTPException(status_code=500, detail=f"AI 服务调用异常: {str(e)}")


# ==================== API 路由 ====================

@router.post("/analyze_log", response_model=LogAnalysisResponse)
async def analyze_log(
    req: LogAnalysisRequest,
    session: Session = Depends(get_session),
):
    """
    智能日志分析接口。

    流程:
    1. 日志清洗降噪 (500行 -> ~80行)
    2. MD5 缓存查询
    3. 调用 LLM 分析
    4. 缓存结果
    """
    if not req.log_text or not req.log_text.strip():
        raise HTTPException(status_code=400, detail="日志内容不能为空")

    if not req.package_name or not req.package_name.strip():
        raise HTTPException(status_code=400, detail="包名不能为空")

    # Step 1: 日志清洗
    cleaned_log = clean_log_for_ai(req.log_text, req.package_name)
    logger.info(f"日志清洗完成: {len(req.log_text.splitlines())} 行 -> {len(cleaned_log.splitlines())} 行")

    # Step 2: MD5 缓存检查
    cache_key = hashlib.md5(cleaned_log.encode('utf-8')).hexdigest()
    if not req.force_refresh and cache_key in _analysis_cache:
        logger.info(f"命中缓存: {cache_key}")
        return LogAnalysisResponse(
            success=True,
            analysis_result=_analysis_cache[cache_key],
            token_usage=0,
            cached=True,
        )

    # Step 3: 调用 LLM
    result = await call_llm_service(
        cleaned_log=cleaned_log,
        package_name=req.package_name,
        device_info=req.device_info or "",
        session=session,
    )

    # Step 4: 写入缓存
    _analysis_cache[cache_key] = result["analysis_result"]
    logger.info(f"分析完成，Token 消耗: {result['token_usage']}，缓存 Key: {cache_key}")

    return LogAnalysisResponse(
        success=True,
        analysis_result=result["analysis_result"],
        token_usage=result["token_usage"],
        cached=False,
    )
