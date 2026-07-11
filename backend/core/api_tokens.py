"""API Token（机器凭证）生成与校验工具。

Token 格式：``adk_`` + secrets.token_hex(24)，共 52 个字符。
数据库中仅存储 sha256 哈希（token_hash），明文只在创建时返回一次。
"""
import hashlib
import hmac
import secrets

API_TOKEN_PREFIX = "adk_"
# 列表页用于识别 token 的展示前缀长度（含 adk_）
API_TOKEN_DISPLAY_PREFIX_LEN = 12


def generate_api_token() -> str:
    """生成新的 API Token 明文：adk_ + 48 位 hex。"""
    return API_TOKEN_PREFIX + secrets.token_hex(24)


def is_api_token(token: str) -> bool:
    """根据前缀判断 Bearer 凭证是否为 API Token（区分于 JWT）。"""
    return isinstance(token, str) and token.startswith(API_TOKEN_PREFIX)


def hash_api_token(token: str) -> str:
    """计算 token 明文的 sha256 哈希（hex），用于存储与查询。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_api_token(token: str, token_hash: str) -> bool:
    """恒时比较 token 明文哈希与存储哈希。"""
    return hmac.compare_digest(hash_api_token(token), token_hash or "")


def token_display_prefix(token: str) -> str:
    """取明文前 12 字符（含 adk_）作为列表识别用前缀。"""
    return token[:API_TOKEN_DISPLAY_PREFIX_LEN]
