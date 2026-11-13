"""
智能日志分析 API

提供：
- Android Crash/ANR 日志清洗降噪算法
- LLM 智能根因分析调用
- MD5 缓存避免重复分析
"""
import re
import asyncio
import hashlib
import logging
from typing import List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.database import get_session
from backend.models import SystemSetting
from backend.openai_compat import (
    LLM_TOTAL_DEADLINE_S,
    extract_message_content,
    parse_chat_completion_payload,
)
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

# 崩溃正文之外还要带上「被测 App 当时在干什么」。线上产物 522 行里只有 21 行带被测
# 包名，只取锚点之后的话这些行全丢，LLM 拿到的就是一个孤立的栈，指不出触发页面。
CONTEXT_MAX_LINES = 24
# 同一条日志形态最多留几行。厂商会把同一句话按帧刷（实测「isLandscapeFullscreenEnabled」
# 连刷十几次），按原文去重拦不住，得先把时间戳/数值抹平再算形态。
CONTEXT_MAX_PER_SHAPE = 2
CONTEXT_PATTERNS = [
    re.compile(r'\b(?:Force finishing|Killing|Start proc|Displayed|Restarting|ANR in|FATAL|not responding|Reason:|onPause|onStop|onDestroy|onCreate)\b'),
    # 这条故意连厂商 tag 一起收：KEYLOG_*PhoneWindowManager* / ViewRootImpl* 会刷屏，
    # 但它们编码的正是 Fastbot 在崩溃前注入的按键序列，是定位触发场景的关键线索。
    # 刷屏由下面的形态收敛去管，不靠这条收紧。
    re.compile(r'WindowManager|Choreographer|ViewRootImpl|System\.err'),
    re.compile(r'Build fingerprint'),
    re.compile(r'\b(?:Process|PID):'),
]

# logcat 行首的设备时间（无年份，但快照窗口只有几秒，够用来比远近）
TIMESTAMP_PATTERN = re.compile(r'^(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})')

# 快照里「正文段」与「上下文段」的分隔行（见 fastbot/logcat.py 的 _compose_snapshot），
# 以及 logcat 自己的 buffer 分隔行。前向扫描撞上它必须停：否则会把上下文段开头一整段
# 当成正文吃进 kept_lines，上下文于是无从挑起，还白占正文预算。
SECTION_SEPARATOR_PATTERN = re.compile(r'^-{3,}\s*(?:logcat\b|beginning of\b)')

# 上游（思考型模型 + max_tokens 吃紧）会返回空正文/非文本正文，这是「点重试即可」
# 的上游问题，走 502 而不是 500；卡顿分析共用同一条提示，保证两端文案一致。
EMPTY_CONTENT_ERROR_DETAIL = "AI 服务未返回可分析的正文（content 为空/非文本），请点「重新分析」重试。"


def _get_setting(session: Session, key: str, default: str = "") -> str:
    """从数据库读取系统配置"""
    setting = session.exec(
        select(SystemSetting).where(SystemSetting.key == key)
    ).first()
    return setting.value if setting else default


# ==================== 日志清洗算法 ====================

def _line_time(line: str) -> Optional[float]:
    """取 logcat 行首的设备时间（秒），取不到返回 None。"""
    match = TIMESTAMP_PATTERN.match(line)
    if not match:
        return None
    month, day, hour, minute, second, millis = (int(part) for part in match.groups())
    # 没有年份：用「月*31+日」当粗略序数，够在几秒宽的窗口里比远近
    return (((month * 31 + day) * 24 + hour) * 60 + minute) * 60 + second + millis / 1000.0


def _line_shape(line: str) -> str:
    """抹掉时间戳与多位数字后的形态，用来认出「同一句话刷屏」。

    只抹两位以上的数字、保留个位数：类名/Activity 名/window id 里的个位数是真差异
    （`WebViewActivity1` 与 `WebViewActivity2` 是两个页面），全抹会把它们误判成一条；
    而 pid/tid/eventTime 这类多位数每次都变，不抹就认不出刷屏。
    """
    return re.sub(r'\d{2,}', '#', TIMESTAMP_PATTERN.sub('', line))


def _collect_context_lines(
    lines: List[str],
    package_name: str,
    already_kept: Set[str],
    anchor_idx: int,
    budget: int,
) -> List[str]:
    """围绕事件时刻挑出「被测 App 当时在干什么」的行，按文件顺序返回。

    只挑与被测包或系统生命周期相关的行：厂商噪音占了绝大多数，全收等于没收。

    远近必须按**时间**算，不能按行号：快照是「正文段在前、上下文段在后」，而
    `logcat -b main,system` 的输出又是每个 buffer 组内各自升序 —— 行号顺序跟时间
    顺序不是一回事。按行号算会挑到离事件最远的那一端，把事件前最近的行全丢。
    """
    if budget <= 0:
        return []

    package_name = (package_name or "").strip()
    anchor_time = _line_time(lines[anchor_idx]) if 0 <= anchor_idx < len(lines) else None

    candidates = []
    for index, line in enumerate(lines):
        key = line.strip()
        if not key or key in already_kept:
            continue
        related = bool(package_name and package_name in line)
        if not related and not any(pattern.search(line) for pattern in CONTEXT_PATTERNS):
            continue
        if anchor_time is not None:
            line_time = _line_time(line)
            # 行首不是标准 logcat 时间（多行 entry 的续行等）当最远
            distance = abs(line_time - anchor_time) if line_time is not None else float('inf')
        else:
            # 锚点自己都没有时间，整份日志的时间口径不可信，退回按行号远近
            distance = abs(index - anchor_idx)
        candidates.append((distance, index, line))

    # 按「离事件多近」挑满预算，同形态的刷屏行只留前几条，再回到文件顺序输出，
    # 让 LLM 能按时间戳还原现场
    candidates.sort(key=lambda item: (item[0], item[1]))
    picked = []
    per_shape: dict = {}
    for distance, index, line in candidates:
        shape = _line_shape(line)
        if per_shape.get(shape, 0) >= CONTEXT_MAX_PER_SHAPE:
            continue
        per_shape[shape] = per_shape.get(shape, 0) + 1
        picked.append((distance, index, line))
        if len(picked) >= budget:
            break

    picked.sort(key=lambda item: item[1])
    return [line for _, _, line in picked]


def _crash_anchor_index(lines: List[str], start_indices: List[int]) -> int:
    """定位「本次崩溃」那一行：取**最后**一个 FATAL EXCEPTION。

    crash buffer 里可能还留着本次运行更早的崩溃，取第一个会围绕错误事件挑上下文。
    与 ANR 路径取 anr_indices[-1] 保持一致；大小写跟 CRASH_ANCHOR_PATTERNS[0] 一样不敏感。
    """
    for index in range(len(lines) - 1, -1, -1):
        if re.search(r'FATAL EXCEPTION', lines[index], re.IGNORECASE):
            return index
    return start_indices[-1] if start_indices else 0


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

            # 撞上段落/buffer 分隔就停：后面是上下文段，不是本次崩溃正文
            if SECTION_SEPARATOR_PATTERN.match(stripped):
                break

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
        return '\n'.join(lines[-80:])

    # 崩溃正文之外补上事件前后的业务上下文，排在正文前面（行内时间戳足够还原顺序）。
    # 正文先占预算，上下文只用剩下的额度 —— 绝不为了上下文去截正文，正文尾部是
    # Caused by 因果链。正文自身的 100 行上限沿用改动前的语义（超出从尾部截）。
    if len(result) > 100:
        result = result[:100]
    context_budget = min(CONTEXT_MAX_LINES, 100 - len(result))
    context_lines = _collect_context_lines(
        lines, package_name, seen, _crash_anchor_index(lines, start_indices), context_budget
    )

    return '\n'.join(context_lines + result)


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

    # 与崩溃路径同一套：正文先占预算，上下文只用剩下的额度，不为上下文截正文
    if len(kept_lines) > 100:
        kept_lines = kept_lines[:100]
    context_budget = min(CONTEXT_MAX_LINES, 100 - len(kept_lines))
    context_lines = _collect_context_lines(
        lines, package_name, seen, start_idx, context_budget
    )
    return '\n'.join(context_lines + kept_lines)


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
            # httpx 的 timeout 是逐次读超时，慢速滴流时总耗时无上限；
            # 用 wait_for 封顶总时长，保证在反代（如 Cloudflare ~100s）掐连接前返回。
            resp = await asyncio.wait_for(
                client.post(
                    request_url,
                    json=payload,
                    headers=headers,
                ),
                timeout=LLM_TOTAL_DEADLINE_S,
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

            # content 可能是 null / 分片数组（思考型模型把预算耗在 reasoning 上），
            # 统一走取正文助手；拿不到正文就在这里报 502，绝不放行到响应模型校验。
            content = extract_message_content(data)
            if not content.strip():
                raise HTTPException(status_code=502, detail=EMPTY_CONTENT_ERROR_DETAIL)

            usage = data.get("usage")
            total_tokens = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0

            return {
                "success": True,
                "analysis_result": content,
                "token_usage": total_tokens,
            }
    except HTTPException:
        raise  # 直接透传已处理的异常
    except (httpx.TimeoutException, asyncio.TimeoutError):
        logger.error("LLM API 调用超时")
        raise HTTPException(status_code=504, detail="AI 服务响应超时，请稍后重试")
    except httpx.ConnectError as e:
        logger.error(f"LLM API 连接失败: {e}")
        raise HTTPException(
            status_code=502,
            detail=f"无法连接 AI 服务。请检查 API 地址 ({api_base}) 是否可访问。"
        )
    except Exception as e:
        # 与 analyze_log 的兜底同一口径：异常文本只进服务端日志，不进 detail
        logger.exception("LLM API 调用异常")
        raise HTTPException(
            status_code=500,
            detail=f"AI 服务调用异常（{type(e).__name__}），请稍后重试",
        )


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
        cached_value = _analysis_cache[cache_key]
        # 历史 bug 会把 None 等脏值写进缓存（一次失败 = 这条日志永久分析不了），
        # 这里自愈：只认非空字符串，脏值丢弃后重新走 LLM。
        if isinstance(cached_value, str) and cached_value.strip():
            logger.info(f"命中缓存: {cache_key}")
            return LogAnalysisResponse(
                success=True,
                analysis_result=cached_value,
                token_usage=0,
                cached=True,
            )
        logger.warning(f"丢弃无效缓存值，重新分析: {cache_key}")
        _analysis_cache.pop(cache_key, None)

    # Step 3~4: 调用 LLM、构造响应、写缓存。
    # 整段再包一层兜底：这条链路上任何漏网异常都必须变成带 detail 的 JSON 错误，
    # 否则 Starlette 会兜底成纯文本 Internal Server Error，前端只看得到「分析失败」。
    try:
        result = await call_llm_service(
            cleaned_log=cleaned_log,
            package_name=req.package_name,
            device_info=req.device_info or "",
            session=session,
        )

        analysis_result = result.get("analysis_result") if isinstance(result, dict) else None
        if not isinstance(analysis_result, str) or not analysis_result.strip():
            raise HTTPException(status_code=502, detail=EMPTY_CONTENT_ERROR_DETAIL)

        # 顺序很关键：先把响应构造出来（此时才做 pydantic 校验），成功了再写缓存。
        # 反过来写会在构造失败时把脏值留进缓存，同一条日志从此一直报错。
        response = LogAnalysisResponse(
            success=True,
            analysis_result=analysis_result,
            token_usage=result.get("token_usage") or 0,
            cached=False,
        )
        _analysis_cache[cache_key] = analysis_result
    except HTTPException:
        raise  # 已经是带 detail 的 JSON 错误，原样透传
    except Exception as exc:
        # 完整异常只进服务端日志：detail 会被前端 normalizeError 原样显示给用户，
        # 把 str(exc) 吐出去等于把内部实现与上游响应细节泄露到界面上。
        logger.exception("日志分析失败")
        raise HTTPException(
            status_code=500,
            detail=f"日志分析失败（{type(exc).__name__}），请重试；反复失败请把时间点反馈给管理员",
        )

    logger.info(f"分析完成，Token 消耗: {response.token_usage}，缓存 Key: {cache_key}")
    return response
