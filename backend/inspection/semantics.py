"""No-resource-id semantic model for Android inspection."""
from __future__ import annotations

import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image


_BOUNDS_RE = re.compile(
    r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]"
)
_SPACE_RE = re.compile(r"\s+")
_DATE_TIME_RE = re.compile(
    r"(?:\b\d{1,2}:\d{2}(?::\d{2})?\b)|"
    r"(?:\b20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?\b)|"
    r"(?:\b\d{1,2}[-/.月]\d{1,2}日?\b)|"
    r"(?:\b\d+\s*(?:秒|分钟|小时|天|sec|min|hour|day)s?\b)",
    re.I,
)
_MONEY_RE = re.compile(
    r"(?:[¥￥$€£]\s*[\d,.]+)|(?:\b[\d,.]+\s*(?:元|美元|usd|cny|rmb)\b)",
    re.I,
)
_RANDOM_VALUE_RE = re.compile(
    r"(?:订单|流水|编号|验证码|会员号|手机号|用户|账号|order|serial|code|id|user)"
    r"[\s:#_-]*[A-Z0-9_-]{4,}",
    re.I,
)
_ONLY_VOLATILE_RE = re.compile(r"^[\W_\d•●*xX#]+$", re.UNICODE)
_LONG_DIGIT_RE = re.compile(r"\d{4,}")
_MASK_RE = re.compile(r"^[*•●·xX_\-\s]+$")
# U+FFFC is an Android accessibility decoration marker and can be removed
# without changing the visible label. U+FFFD means decoding already lost data
# and must still invalidate a replay locator.
_INVALID_ACCESSIBILITY_TEXT_RE = re.compile(r"\uFFFD")
_DYNAMIC_COUNTER_RE = re.compile(
    r"(?:活动页|页码|购物车|消息|通知|未读|数量|库存|"
    r"activity\s*page|page|cart|message|notification|unread|count|stock)"
    r"\s*[,，:：#_\-(（]?\s*\d+\s*[)）]?(?!\d)",
    re.I,
)
_NAVIGATION_ACTION_RE = re.compile(
    r"(?:加入购物车|立即购买|立即支付|去结算|提交订单|确认订单|"
    r"add\s+to\s+cart|buy\s+now|pay\s+now|checkout|place\s+order)",
    re.I,
)
_DIALOG_CLASS_RE = re.compile(r"dialog|popup|menu|modal", re.I)
_MODAL_CLASS_RE = re.compile(r"dialog|popup|modal", re.I)
_OPAQUE_CLASS_RE = re.compile(r"webview|surface(?:view)?|canvas|mapview", re.I)
_INDICATOR_HINT_RE = re.compile(r"indicator|underline|selection|选中|指示", re.I)
_DECORATIVE_ITEM_LABEL_RE = re.compile(
    r"^(?:自营|权益|券|包邮|新品|热卖|推荐|促销|"
    r"\d+\s*级|self[-\s]?operated|benefit|coupon)$",
    re.I,
)
_PRODUCT_SPEC_LABEL_RE = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)?\s*(?:kg|公斤|升|l|级|匹|人|年|mm|cm)?|"
    r"(?:一级|二级|三级|1级|2级|3级)?(?:能耗|能效等级)|"
    r"(?:变频|定频|风冷|直冷|自营|权益|销量|已买|加购|到手价|电机类型|"
    r"压缩机类型|制冷方式|箱门结构|洗涤容量(?:（kg）|\(kg\))?)"
    r")$",
    re.I,
)
_NON_TITLE_CARD_LABEL_RE = re.compile(
    r"^(?:距离最近|离我最近|进店逛逛|立即预约|门店电话|"
    r"nearest|visit\s+store|book|call)$",
    re.I,
)
_STORE_LIST_RE = re.compile(
    r"附近门店|线下专卖店|立即预约|门店电话|进店逛逛|"
    r"\d+(?:\.\d+)?\s*km|store\s+nearby|visit\s+store",
    re.I,
)
_SERVICE_LIST_RE = re.compile(
    r"深度清洗|原厂服务|清洗服务|上门清洗|(?:冰箱|空调|洗衣机)清洗",
    re.I,
)
_SERVICE_DETAIL_OFFERING_RE = re.compile(
    r"^(?:"
    r"(?:(?:haier\s*/\s*)?海尔[,，]?\s*)?"
    r"(?:家电|整机|空调|冰箱|洗衣机|热水器|油烟机|燃气灶|灶具|"
    r"净水机|饮水机|电视|洗碗机|干衣机|烘干机|柜机|挂机)?"
    r"(?:深度清洗|清洗服务|上门(?:清洗|维修|安装)?服务|"
    r"延保(?:服务|套餐|卡|权益)?|延长保修(?:服务|套餐)?)"
    r"(?:套餐|服务卡)?|"
    r"(?:deep\s+clean(?:ing)?|home\s+service|extended\s+warranty)"
    r"(?:\s+service)?|海尔原厂延保"
    r")$",
    re.I,
)
_SERVICE_DETAIL_PROVIDER_RE = re.compile(
    r"^(?:海尔服务|家生活服务|海尔原厂延保|haier\s+service)$",
    re.I,
)
_SERVICE_DETAIL_PROVIDER_OFFERING_RE = re.compile(
    r"^(?:海尔原厂延保|(?:海尔服务|家生活服务|haier\s+service)\s+.{0,32}"
    r"(?:深度清洗|清洗服务|上门(?:清洗|维修|安装)?服务|"
    r"延保|延长保修|deep\s+clean|home\s+service|extended\s+warranty))$",
    re.I,
)
_SERVICE_DETAIL_TERMS_RE = re.compile(
    r"(?:本服务仅限|服务(?:内容|流程|范围|区域|须知|说明|标准)|"
    r"预约须知|上门时间|服务次数|延保期限|service\s+(?:terms|scope|process))",
    re.I,
)
_SERVICE_DETAIL_ACTION_RE = re.compile(
    r"^(?:立即预约|预约服务|购买服务|立即购买服务|预约上门|"
    r"book\s+service|buy\s+service)$",
    re.I,
)
_CONSUMABLE_LIST_RE = re.compile(r"滤芯|filter\s*(?:element|cartridge)", re.I)
_PRODUCT_LIST_RE = re.compile(r"洗护清洁|洗衣液|清洁工具|护理剂", re.I)
_HAIER_APPLIANCE_RE = re.compile(
    r"冰箱|冷柜|洗衣机|洗烘|空调|热水器|净水|厨电|电视",
    re.I,
)
_FILTER_PANEL_FACETS = {
    "商品",
    "价格",
    "尺寸",
    "嵌入方式",
    "能效等级",
    "门款式",
    "总容积",
    "品牌",
    "是否app控制",
    "变频/定频",
}
_PRODUCT_DETAIL_ACTION_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("BACK", re.compile(r"^(?:返回|后退|back)$", re.I)),
    ("SHARE", re.compile(r"^(?:分享|share)$", re.I)),
    ("MEDIA_PLAY", re.compile(r"(?:播放|商品视频|主图视频|play|video)", re.I)),
    ("FAVORITE", re.compile(r"(?:收藏|取消收藏|favorite|wishlist)", re.I)),
    ("CART_OPEN", re.compile(r"^(?:购物车|cart)(?:\s*[,，(（]?\s*\d+\s*[)）]?)?$", re.I)),
    ("ADD_CART", re.compile(r"(?:加入购物车|add\s+to\s+cart)", re.I)),
    ("BUY_NOW", re.compile(r"(?:立即购买|buy\s+now)", re.I)),
    ("ARRIVAL_NOTICE", re.compile(r"(?:到货通知|缺货登记|到货提醒|notify\s+me)", re.I)),
    (
        "OPTION_SELECT",
        re.compile(
            r"^(?:已选|选择规格|规格|型号|颜色|尺寸|option|variant)"
            r"(?:\s*[,，:：]\s*.+)?$",
            re.I,
        ),
    ),
    ("ADDRESS_OPEN", re.compile(r"^(?:地址|配送至|送至|收货地区|address|deliver\s+to)$", re.I)),
    ("SERVICE_OPEN", re.compile(r"^(?:服务|保障|售后|配送服务|service|warranty)$", re.I)),
)
_PRODUCT_DETAIL_FAMILY_IDENTITY_ROLES = {
    "BACK",
    "CART_OPEN",
    "ADD_CART",
    "BUY_NOW",
    "ARRIVAL_NOTICE",
    "OPTION_SELECT",
    "SCROLL_CONTAINER",
}
_PRODUCT_DETAIL_TRANSACTION_CAPABILITY_ROLES = {
    "ADD_CART",
    "BUY_NOW",
    "ARRIVAL_NOTICE",
    "OPTION_SELECT",
}
_INSTANCE_ACTION_ROLE_PREFIX = "INSTANCE:"
_REPEATED_CARD_TRANSACTION_RE = re.compile(
    r"立即购买|加入购物车|到货通知|进店逛逛|"
    r"buy\s+now|add\s+to\s+cart|notify\s+me|visit\s+store",
    re.I,
)
_SORT_LABELS = {
    "综合": "default",
    "销量": "sales",
    "价格": "price",
    "上新": "newest",
    "默认": "default",
    "comprehensive": "default",
    "sales": "sales",
    "price": "price",
    "newest": "newest",
}

_FAMILY_STRUCTURE_THRESHOLD = 0.94
_FAMILY_ACTION_THRESHOLD = 0.90
_FAMILY_LAYOUT_THRESHOLD = 0.90
_FAMILY_SCORE_THRESHOLD = 0.93
_MIN_NORMALIZED_ACTION_DIMENSION = 0.005

_NAVIGATION_MIN_MEMBERS = 2
_NAVIGATION_MAX_MEMBERS = 7
_BOTTOM_NAVIGATION_MIN_MEMBERS = 3
_BOTTOM_NAVIGATION_MIN_COVERAGE = 0.70
_NAVIGATION_COORDINATE_TOLERANCE = 0.05
_NAVIGATION_LABEL_OVERLAP = 0.80
_PEER_NAVIGATION_CONFIDENCE = 0.85

_PAGE_ROLE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "CHECKOUT",
        re.compile(
            r"确认订单|提交订单|去结算|收货地址|配送方式|订单结算|"
            r"checkout|place\s*order|shipping\s*address",
            re.I,
        ),
    ),
    (
        "ORDER",
        re.compile(
            r"我的订单|订单详情|订单列表|订单状态|查看订单|"
            r"my\s*orders?|order\s*(?:detail|history|status)",
            re.I,
        ),
    ),
    (
        "PRODUCT_DETAIL",
        re.compile(
            r"商品详情|产品详情|加入购物车|立即购买|到货通知|商品参数|"
            r"product\s*detail|add\s*to\s*cart|buy\s*now",
            re.I,
        ),
    ),
    (
        "LIST",
        re.compile(
            r"购物车|搜索结果|搜索|筛选|分类|列表|全部商品|cart|"
            r"search\s*results?|filter|category|catalog|\blist\b",
            re.I,
        ),
    ),
    ("HOME", re.compile(r"首页|主页|home", re.I)),
)

_ACTIVITY_SUFFIXES = {
    "activity",
    "controller",
    "fragment",
    "page",
    "screen",
    "view",
}

_FUZZY_PAGE_ROLES = {
    "PRODUCT_DETAIL",
    "CHECKOUT",
    "ORDER",
    "LIST",
    "HOME",
    "PROFILE",
    "SETTINGS",
    "ADDRESS_LIST",
}

_PROFILE_SURFACE_SIGNAL_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:我的社区|消息|客服|设置)$", re.I),
    re.compile(r"(?:优惠券|我的卡包|我的积分)", re.I),
    re.compile(r"^(?:待付款|待发货|待收货|评价有礼|退款/售后)(?:[,，\s].*)?$", re.I),
    re.compile(r"^(?:商品收藏|历史浏览)(?:[,，\s].*)?$", re.I),
    re.compile(r"^(?:家电安装|家电维修|进度查询|服务卡激活|收费标准|常见故障)$", re.I),
)
_COMMUNITY_FEED_DATE_RE = re.compile(r"\b\d{2}-\d{2}\s+\d{2}:\d{2}\b")
_HAIER_MALL_PACKAGES = frozenset({"com.ehaier.zgq.shop.mall"})
_SETTINGS_SIGNAL_RE = re.compile(
    r"账号与安全|账号关联|隐私设置|意见反馈|关于海尔商城|清除本地缓存|退出登录",
    re.I,
)
_ADDRESS_LIST_SIGNAL_RE = re.compile(r"修改|新建收货地址|全部|家|公司|学校", re.I)
_ADDRESS_FORM_SIGNAL_RE = re.compile(
    r"收货人姓名|所在地区|详细地址|地址标签|默认地址|一键定位|保存",
    re.I,
)
_INVOICE_FORM_SIGNAL_RE = re.compile(
    r"发票类型|抬头类型|发票抬头|确定添加|请输入个人/单位名称|设置为默认",
    re.I,
)

_SEMANTIC_KIND_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("BACK", re.compile(r"^(?:返回|后退|back)$", re.I)),
    ("CLOSE", re.compile(r"^(?:关闭|取消关闭|close|dismiss)$", re.I)),
    ("CONFIRM", re.compile(r"确认|确定|完成|confirm|done|ok", re.I)),
    ("CANCEL", re.compile(r"取消|撤销|cancel", re.I)),
    ("NEXT", re.compile(r"下一步|继续|next|continue", re.I)),
    ("PREVIOUS", re.compile(r"上一步|previous|prev", re.I)),
    ("SAVE", re.compile(r"保存|save", re.I)),
    ("DELETE", re.compile(r"删除|移除|delete|remove", re.I)),
    ("SEARCH", re.compile(r"搜索|search", re.I)),
    ("FILTER", re.compile(r"筛选|filter", re.I)),
    ("ADD_CART", re.compile(r"加入购物车|add\s+to\s+cart", re.I)),
    ("BUY_NOW", re.compile(r"立即购买|buy\s+now", re.I)),
    ("CHECKOUT", re.compile(r"去结算|结算|checkout", re.I)),
    ("PLACE_ORDER", re.compile(r"提交订单|确认订单|place\s*order", re.I)),
    ("PAY", re.compile(r"立即支付|支付|pay", re.I)),
    ("LOGIN", re.compile(r"登录|sign\s*in|log\s*in", re.I)),
    ("REGISTER", re.compile(r"注册|register|sign\s*up", re.I)),
    ("CART", re.compile(r"^(?:购物车|cart)$", re.I)),
    ("HOME", re.compile(r"^(?:首页|主页|home)$", re.I)),
    ("ORDER", re.compile(r"订单|orders?", re.I)),
    ("SETTINGS", re.compile(r"设置|settings?", re.I)),
    ("PROFILE", re.compile(r"我的|账户|个人中心|profile|account", re.I)),
)

_DEFAULT_BLOCK_RULES: Tuple[Tuple[str, str], ...] = (
    (
        "DESTRUCTIVE",
        r"删除|清空|注销|退出登录|销户|取消(?:预约|订单|服务)|"
        r"delete|remove|clear|logout|log\s*out|sign\s*out|cancel\s+(?:booking|order|service)",
    ),
    (
        "EXTERNAL_SIDE_EFFECT",
        r"发送|发布|拨号|呼叫|(?:门店|联系)?电话|提交预约|确认预约|"
        r"send|publish|post|dial|call|phone",
    ),
    (
        "SYSTEM_OR_EXTERNAL",
        r"系统设置|打开其他应用|system\s*settings|open\s+with|permission|"
        r"(?:^|[|:：]\s*)(?:安装|卸载)(?:应用|软件|程序|未知来源应用)?(?=\s*(?:[|]|$))|"
        r"(?:^|[|:：]\s*)(?:install|uninstall)(?:\s+(?:app|application|package|software))?"
        r"(?=\s*(?:[|]|$))",
    ),
)

_HAIER_MALL_PACKAGE = "com.ehaier.zgq.shop.mall"
_HAIER_CASHIER_ANCHOR_RE = re.compile(
    r"^(?:海尔收银台|haier\s+cashier)(?:\s*[-—|:：]\s*.{1,24})?$",
    re.I,
)
_FINAL_PAYMENT_ACTION_RE = re.compile(
    r"(?:"
    r"确认[^|\r\n]{0,24}(?:支付|付款)(?:订单)?"
    r"(?=$|[\s,，:：;；。.!！?？()（）¥￥$]|并|后)|"
    r"(?:立即|去)(?:支付|付款)(?:订单)?"
    r"(?=$|[\s,，:：;；。.!！?？()（）¥￥$]|并|后)|"
    r"(?:^|[\s,，:：;；()（）])(?:支付|付款)(?:订单)?"
    r"(?=$|[\s,，:：;；。.!！?？()（）¥￥$]|并|后)|"
    r"\b(?:pay\s+now|confirm(?:\s+\S+){0,4}\s+(?:pay|payment)|"
    r"(?:complete|submit)\s+(?:pay|payment)|make\s+(?:a\s+)?payment)\b"
    r")",
    re.I,
)
_NON_COMMIT_PAYMENT_ACTION_RE = re.compile(
    r"^(?:"
    r"(?:(?:选择|更换|查看|设置|确认|同意)\s*)?"
    r"支付(?:方式|说明|协议|帮助|渠道|工具|密码)|"
    r"(?:(?:select|change|view|set|confirm|accept)\s+)?"
    r"payment\s+(?:method|methods|option|options|instruction|instructions|"
    r"agreement|terms|help|channel|channels|password)"
    r")(?:\s*[:：,，-].*)?$",
    re.I,
)
_SAFETY_CONTEXT_RESOURCE_RE = re.compile(
    r"(?:^|[/_.:-])(?:title|toolbar|header|dialog|message)(?:$|[/_.:-])",
    re.I,
)
_HAIER_CASHIER_ACTIVITY_RE = re.compile(
    r"(?:haier[_.$-]*)?(?:cashier|cash[_.$-]*desk|payment[_.$-]*cashier)",
    re.I,
)
_SEMANTIC_IGNORABLE_TRANSLATION = str.maketrans(
    "", "", "\u200b\u200c\u200d\ufeff\uFFFC"
)
_SAFETY_IGNORABLE_TRANSLATION = _SEMANTIC_IGNORABLE_TRANSLATION
_INSTANCE_ENTRY_LABEL_SPLIT_RE = re.compile(r"(?:\r?\n+|[,，;；|｜]+)")

_SENSITIVE_HINT_RE = re.compile(
    r"密码|口令|验证码|身份证|银行卡|手机号|token|secret|password|passwd|pin|otp|"
    r"credit\s*card|bank\s*card|phone",
    re.I,
)


def _attr(element: ET.Element, *names: str) -> str:
    for name in names:
        value = element.attrib.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _bool_attr(element: ET.Element, name: str, default: bool = False) -> bool:
    value = str(element.attrib.get(name, "")).strip().lower()
    if not value:
        return default
    return value == "true"


def parse_bounds(value: Any) -> Optional[Tuple[int, int, int, int]]:
    match = _BOUNDS_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    x1, y1, x2, y2 = (int(item) for item in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def normalize_semantic_text(value: Any) -> str:
    normalized = str(value or "").translate(_SEMANTIC_IGNORABLE_TRANSLATION)
    return _SPACE_RE.sub(" ", normalized.strip())


def _normalize_safety_text(value: Any) -> str:
    return normalize_semantic_text(value).translate(_SAFETY_IGNORABLE_TRANSLATION)


def _stable_primary_action_label(
    *,
    text: Any = "",
    content_desc: Any = "",
    fallback: Any = "",
) -> str:
    """Normalize one business label without retaining full-card accessibility copy."""
    explicit_text = _normalize_safety_text(text)
    if explicit_text:
        return explicit_text
    description = _normalize_safety_text(content_desc)
    if description:
        segments = [
            normalize_semantic_text(segment)
            for segment in _INSTANCE_ENTRY_LABEL_SPLIT_RE.split(description)
            if normalize_semantic_text(segment)
        ]
        return next(
            (
                segment
                for segment in segments
                if re.search(r"[A-Za-z\u4e00-\u9fff]", segment)
                and not re.fullmatch(r"(?:张|个|件|条|分|元)", segment)
            ),
            segments[0] if segments else "",
        )
    return _normalize_safety_text(fallback)


def _is_final_payment_action(value: Any) -> bool:
    text = _normalize_safety_text(value)
    if not text or _NON_COMMIT_PAYMENT_ACTION_RE.fullmatch(text):
        return False
    return bool(_FINAL_PAYMENT_ACTION_RE.search(text))


def is_stable_semantic_text(
    value: Any,
    *,
    max_length: int = 80,
    dynamic_patterns: Optional[Sequence[str]] = None,
) -> bool:
    """Whether a description/text is safe to use as a replay locator."""
    text = normalize_semantic_text(value)
    if not text or len(text) > max_length:
        return False
    # U+FFFC object replacement and U+FFFD replacement characters are
    # accessibility rendering artifacts, not stable user-visible semantics.
    # UiAutomator may expose them in an XML snapshot but cannot reliably
    # resolve the same value through a text selector afterwards.
    if _INVALID_ACCESSIBILITY_TEXT_RE.search(text):
        return False
    if _MASK_RE.fullmatch(text) or _ONLY_VOLATILE_RE.fullmatch(text):
        return False
    if _DATE_TIME_RE.search(text) or _MONEY_RE.search(text) or _RANDOM_VALUE_RE.search(text):
        return False
    if _DYNAMIC_COUNTER_RE.search(text):
        return False
    # Long numeric values are usually counters, IDs, account data or timestamps.
    if _LONG_DIGIT_RE.search(text):
        return False
    for pattern in dynamic_patterns or ():
        try:
            if re.search(str(pattern), text, re.I):
                return False
        except re.error:
            continue
    return True


def screenshot_sha(png_bytes: bytes) -> str:
    return hashlib.sha256(png_bytes or b"").hexdigest()


def perceptual_hash(png_bytes: bytes, hash_size: int = 8) -> str:
    """Small deterministic average hash used only for animation tolerance."""
    if not png_bytes:
        return ""
    with Image.open(io.BytesIO(png_bytes)) as image:
        grayscale = image.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
        pixels = list(grayscale.getdata())
    average = sum(pixels) / max(len(pixels), 1)
    bits = "".join("1" if pixel >= average else "0" for pixel in pixels)
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def phash_distance(left: str, right: str) -> int:
    if not left or not right or len(left) != len(right):
        return 10**9
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError:
        return 10**9


@dataclass(frozen=True)
class SemanticNode:
    ordinal: int
    element: ET.Element
    parent_ordinal: Optional[int]
    path: Tuple[int, ...]
    class_name: str
    node_package: str
    content_desc: str
    text: str
    resource_id: str
    bounds: Optional[Tuple[int, int, int, int]]
    clickable: bool
    enabled: bool
    visible: bool
    editable: bool
    scrollable: bool
    checked: bool
    selected: bool
    password: bool
    stable_desc: str
    stable_text: str

    @property
    def semantic(self) -> str:
        return self.stable_desc or self.stable_text


@dataclass
class PageModel:
    xml: str
    package_name: str
    activity: str
    nodes: List[SemanticNode]
    cluster_key: str
    replay_key: str
    state_key: str
    is_opaque: bool
    has_dynamic_text: bool
    role: str = "UNKNOWN"
    page_subtype: str = "UNKNOWN"
    template_key: str = ""
    semantic_key: str = ""
    activity_family: str = ""
    screenshot_phash: str = ""
    template_tokens: Tuple[str, ...] = field(default_factory=tuple, repr=False)
    action_tokens: Tuple[str, ...] = field(default_factory=tuple, repr=False)
    landmark_keys: Tuple[str, ...] = field(default_factory=tuple, repr=False)
    control_tokens: Tuple[str, ...] = field(default_factory=tuple, repr=False)
    risk_tokens: Tuple[str, ...] = field(default_factory=tuple, repr=False)
    _by_ordinal: Dict[int, SemanticNode] = field(default_factory=dict, repr=False)

    def node(self, ordinal: Optional[int]) -> Optional[SemanticNode]:
        if ordinal is None:
            return None
        return self._by_ordinal.get(ordinal)

    @property
    def signature(self) -> Dict[str, Any]:
        """Return the screenshot-free, persistence-safe identity evidence."""
        return {
            "version": 2,
            "role": self.role,
            "page_subtype": self.page_subtype,
            "activity_family": self.activity_family,
            "template_key": self.template_key,
            "semantic_key": self.semantic_key,
            "template_tokens": list(self.template_tokens),
            "action_tokens": list(self.action_tokens),
            "landmark_keys": list(self.landmark_keys),
            "control_tokens": list(self.control_tokens),
            "risk_tokens": list(self.risk_tokens),
        }


@dataclass(frozen=True)
class PageSimilarity:
    """Conservative, persistence-safe evidence for a possible logical match."""

    score: float
    equivalent: bool
    same_template: bool
    candidate_semantic_key: str
    candidate_template_key: str
    evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "equivalent": self.equivalent,
            "same_template": self.same_template,
            "candidate_semantic_key": self.candidate_semantic_key,
            "candidate_template_key": self.candidate_template_key,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ExplorationFamilySimilarity:
    """Evidence that two distinct business states share one exploration plan."""

    score: float
    equivalent: bool
    family_key: str
    evidence: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "equivalent": self.equivalent,
            "family_key": self.family_key,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class InspectionAction:
    action_type: str
    action_key: str
    locator_candidates: List[Dict[str, Any]]
    target_meta: Dict[str, Any]
    coordinate_only: bool = False
    replayable: bool = True
    risk_type: Optional[str] = None
    blocked_reason: Optional[str] = None
    input_rule_id: Optional[str] = None
    input_variable_key: Optional[str] = None
    action_role: Optional[str] = None
    action_role_key: Optional[str] = None
    action_anchor_key: Optional[str] = None
    action_group_key: Optional[str] = None
    action_instance_key: Optional[str] = None
    sample_policy: str = "ALL"


@dataclass(frozen=True)
class NavigationMember:
    """One clickable member of a navigation group.

    Bounds are normalized so the metadata can be compared across devices and
    hierarchy captures without retaining the source XML.
    """

    label: str
    index: int
    node_ordinal: int
    class_name: str
    normalized_bounds: Tuple[float, float, float, float]
    selected: bool = False
    checked: bool = False
    has_indicator: bool = False

    @property
    def active(self) -> bool:
        return self.selected or self.checked or self.has_indicator

    @property
    def member_key(self) -> str:
        return _hash_payload(
            {"navigation_member": normalize_semantic_text(self.label).casefold()}
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return persistence-safe evidence without user-visible labels."""
        return {
            "member_key": self.member_key,
            "index": self.index,
            "class_key": _hash_payload({"class": self.class_name}),
            "normalized_bounds": list(self.normalized_bounds),
            "selected": self.selected,
            "checked": self.checked,
            "has_indicator": self.has_indicator,
            "active": self.active,
        }


@dataclass(frozen=True)
class NavigationGroup:
    """Serializable, geometry-backed navigation candidate."""

    group_key: str
    region: str
    parent_ordinal: int
    parent_class: str
    coverage: float
    normalized_bounds: Tuple[float, float, float, float]
    members: Tuple[NavigationMember, ...]
    candidate_confidence: float

    @property
    def labels(self) -> Tuple[str, ...]:
        return tuple(member.label for member in self.members)

    @property
    def active_member_count(self) -> int:
        return sum(1 for member in self.members if member.active)

    @property
    def signature(self) -> Dict[str, Any]:
        return {
            "region": self.region,
            "member_count": len(self.members),
            "member_keys": [member.member_key for member in self.members],
            "normalized_bounds": list(self.normalized_bounds),
            "structure_key": _hash_payload(
                {
                    "parent_class": self.parent_class,
                    "member_classes": [member.class_name for member in self.members],
                }
            ),
            "member_centers": [
                [
                    round((member.normalized_bounds[0] + member.normalized_bounds[2]) / 2, 4),
                    round((member.normalized_bounds[1] + member.normalized_bounds[3]) / 2, 4),
                ]
                for member in self.members
            ],
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_key": self.group_key,
            "region": self.region,
            "coverage": round(self.coverage, 4),
            "normalized_bounds": list(self.normalized_bounds),
            "candidate_confidence": round(self.candidate_confidence, 4),
            "active_member_count": self.active_member_count,
            "group_signature": self.signature,
            "members": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class NavigationConfirmation:
    """Result of confirming that a click changed to a peer Tab page."""

    matched: bool
    confidence: float
    group_key: str
    evidence: Dict[str, Any]
    target_group: Optional[NavigationGroup] = None

    @property
    def is_peer(self) -> bool:
        return self.matched

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "matched": self.matched,
            "is_peer": self.matched,
            "confidence": round(self.confidence, 4),
            "group_key": self.group_key or None,
            "evidence": dict(self.evidence),
        }
        if self.target_group is not None:
            result["target_group_evidence"] = {
                "group_key": self.target_group.group_key,
                "region": self.target_group.region,
                "member_count": len(self.target_group.members),
                "normalized_bounds": list(self.target_group.normalized_bounds),
                "active_member_indices": [
                    member.index
                    for member in self.target_group.members
                    if member.active
                ],
            }
        return result


def _walk_nodes(
    root: ET.Element,
    *,
    dynamic_patterns: Optional[Sequence[str]],
    max_text_length: int,
) -> List[SemanticNode]:
    nodes: List[SemanticNode] = []
    by_element: Dict[int, int] = {}

    def visit(element: ET.Element, parent_ordinal: Optional[int], path: Tuple[int, ...]) -> None:
        ordinal = len(nodes)
        class_name = _attr(element, "class") or str(element.tag or "")
        content_desc = normalize_semantic_text(
            _attr(element, "content-desc", "contentDescription", "description")
        )
        text = normalize_semantic_text(_attr(element, "text", "value", "label"))
        bounds = parse_bounds(_attr(element, "bounds"))
        enabled = _bool_attr(element, "enabled", True)
        visible = (
            not _bool_attr(element, "invisible", False)
            and str(element.attrib.get("displayed", "true")).lower() != "false"
            and str(element.attrib.get("visible-to-user", "true")).lower()
            != "false"
            and bounds is not None
        )
        stable_desc = (
            content_desc
            if is_stable_semantic_text(
                content_desc,
                max_length=max_text_length,
                dynamic_patterns=dynamic_patterns,
            )
            else ""
        )
        stable_text = (
            text
            if is_stable_semantic_text(
                text,
                max_length=max_text_length,
                dynamic_patterns=dynamic_patterns,
            )
            else ""
        )
        editable = (
            _bool_attr(element, "editable", False)
            or class_name.endswith("EditText")
            or (
                _bool_attr(element, "focusable", False)
                and any(token in class_name.lower() for token in ("input", "edit", "textfield"))
            )
        )
        node = SemanticNode(
            ordinal=ordinal,
            element=element,
            parent_ordinal=parent_ordinal,
            path=path,
            class_name=class_name,
            node_package=_attr(element, "package"),
            content_desc=content_desc,
            text=text,
            resource_id=_attr(element, "resource-id", "resourceId"),
            bounds=bounds,
            clickable=_bool_attr(element, "clickable", False),
            enabled=enabled,
            visible=visible,
            editable=editable,
            scrollable=_bool_attr(element, "scrollable", False),
            checked=_bool_attr(element, "checked", False),
            selected=_bool_attr(element, "selected", False),
            password=_bool_attr(element, "password", False),
            stable_desc=stable_desc,
            stable_text=stable_text,
        )
        nodes.append(node)
        by_element[id(element)] = ordinal
        for index, child in enumerate(list(element)):
            visit(child, ordinal, path + (index,))

    visit(root, None, ())
    return nodes


def _children_by_parent(nodes: Sequence[SemanticNode]) -> Dict[Optional[int], List[SemanticNode]]:
    result: Dict[Optional[int], List[SemanticNode]] = {}
    for node in nodes:
        result.setdefault(node.parent_ordinal, []).append(node)
    return result


def _canonical_tree(
    nodes: Sequence[SemanticNode],
    *,
    include_state: bool,
) -> List[Dict[str, Any]]:
    local_ordinals = {
        node.ordinal: index
        for index, node in enumerate(nodes)
    }
    by_ordinal = {node.ordinal: node for node in nodes}

    def local_parent(node: SemanticNode) -> Optional[int]:
        parent_ordinal = node.parent_ordinal
        while parent_ordinal is not None:
            if parent_ordinal in local_ordinals:
                return local_ordinals[parent_ordinal]
            parent = by_ordinal.get(parent_ordinal)
            parent_ordinal = parent.parent_ordinal if parent is not None else None
        return None

    canonical: List[Dict[str, Any]] = []
    for node in nodes:
        # resource-id, focused, absolute index and absolute bounds intentionally
        # do not participate in either identity.
        item: Dict[str, Any] = {
            "parent": local_parent(node),
            "class": node.class_name,
            "desc": node.stable_desc,
            "text": node.stable_text,
        }
        if include_state:
            item.update(
                {
                    "checked": node.checked,
                    "selected": node.selected,
                    "enabled": node.enabled,
                }
            )
        canonical.append(item)
    return canonical


def _identity_nodes(
    nodes: Sequence[SemanticNode],
    package_name: str,
) -> List[SemanticNode]:
    """Keep status bars and vendor overlays out of target-app state identity.

    Android hierarchy dumps may contain several top-level windows.  Their node
    counts and text change independently of the app, so including them makes a
    stable app page appear different on every capture.  Keep the full node list
    for safety/action discovery, but scope cluster/state identity to the target
    package whenever the dump exposes package metadata.
    """
    target = str(package_name or "").strip().lower()
    if not target:
        return list(nodes)
    matching = [
        node
        for node in nodes
        if str(node.node_package or "").strip().lower() == target
    ]
    return matching or list(nodes)


def _coverage_identity_nodes(
    nodes: Sequence[SemanticNode],
    package_name: str,
) -> List[SemanticNode]:
    """Remove Haier's rotating search hint from identity, not from evidence."""
    scoped = _identity_nodes(nodes, package_name)
    if normalize_semantic_text(package_name).casefold() not in _HAIER_MALL_PACKAGES:
        return scoped
    page_width = max((node.bounds[2] for node in scoped if node.bounds), default=0)
    page_height = max((node.bounds[3] for node in scoped if node.bounds), default=0)
    if page_width <= 0 or page_height <= 0:
        return scoped
    has_catalog_sidebar = bool(_catalog_sidebar_members(scoped))
    header_paths: list[Tuple[int, ...]] = []
    for node in scoped:
        if not node.visible or not node.clickable or node.bounds is None:
            continue
        left, top, right, bottom = node.bounds
        header_geometry = bool(
            left / page_width <= 0.10
            and right / page_width >= 0.80
            and top / page_height <= 0.08
            and bottom / page_height <= 0.12
            and (right - left) / page_width >= 0.70
            and 0.025 <= (bottom - top) / page_height <= 0.08
        )
        has_editable_descendant = any(
            candidate.editable
            and len(candidate.path) > len(node.path)
            and candidate.path[: len(node.path)] == node.path
            for candidate in scoped
        )
        if header_geometry and (has_catalog_sidebar or has_editable_descendant):
            header_paths.append(node.path)
    if not header_paths:
        return scoped
    return [
        replace(node, stable_desc="", stable_text="")
        if any(
            len(node.path) >= len(path) and node.path[: len(path)] == path
            for path in header_paths
        )
        else node
        for node in scoped
    ]


def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _activity_words(activity: Any) -> Tuple[str, ...]:
    raw = normalize_semantic_text(activity)
    if not raw:
        return ()
    component = raw.rsplit("/", 1)[-1]
    simple_name = component.rsplit(".", 1)[-1]
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", simple_name)
    return tuple(
        token.casefold()
        for token in re.findall(r"[A-Za-z]+|[0-9]+|[\u4e00-\u9fff]+", separated)
    )


def _activity_family(activity: Any) -> str:
    words = list(_activity_words(activity))
    while words and (words[-1] in _ACTIVITY_SUFFIXES or words[-1].isdigit()):
        words.pop()
    return "_".join(words)


def _activity_role_text(activity: Any) -> str:
    return " ".join(_activity_words(activity))


def _is_profile_surface(nodes: Sequence[SemanticNode]) -> bool:
    values = {
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    matched_groups = {
        index
        for index, pattern in enumerate(_PROFILE_SURFACE_SIGNAL_PATTERNS)
        if any(pattern.search(value) for value in values)
    }
    return len(matched_groups) >= 4


def _is_community_feed_surface(nodes: Sequence[SemanticNode]) -> bool:
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    has_community_context = any(
        re.search(r"许愿池.{0,12}社区|社区.{0,12}伙伴", value, re.I)
        for value in values
    )
    dated_entries = sum(
        1 for value in values if _COMMUNITY_FEED_DATE_RE.search(value)
    )
    return has_community_context and dated_entries >= 2


def _is_haier_community_detail_surface(nodes: Sequence[SemanticNode]) -> bool:
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    has_article_date = any(_COMMUNITY_FEED_DATE_RE.search(value) for value in values)
    has_comment_composer = any(
        re.fullmatch(r"来说点什么(?:\.{3}|…)?", value, re.I)
        for value in values
    )
    article_signals = sum(
        any(re.search(pattern, value, re.I) for value in values)
        for pattern in (
            r"^官方$",
            r"^精选$|参与话题",
            r"获奖信息|亲爱的海尔家人|许愿池.{0,12}伙伴",
        )
    )
    return bool(has_article_date and has_comment_composer and article_signals >= 2)


def _is_haier_member_benefits_surface(nodes: Sequence[SemanticNode]) -> bool:
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    joined = " | ".join(values[:240])
    smart_life_benefits = bool(
        re.search(r"Smart\s*life", joined, re.I)
        and re.search(r"满减券|会员礼包|会员专享", joined, re.I)
        and re.search(r"去使用|立即领取", joined, re.I)
    )
    rights_hub = bool(
        _top_visible_title(nodes, re.compile(r"我的权益", re.I))
        and sum(
            bool(re.search(pattern, joined, re.I))
            for pattern in (r"积分", r"优惠券|卡包", r"生态权益|活动实物|购机赠礼")
        )
        >= 2
    )
    return smart_life_benefits or rights_hub


def _is_haier_favorites_surface(nodes: Sequence[SemanticNode]) -> bool:
    if not _top_visible_title(nodes, re.compile(r"(?:商品收藏|我的收藏)", re.I)):
        return False
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    joined = " | ".join(values[:240])
    return bool(
        re.search(r"管理", joined, re.I)
        and re.search(r"全部\s*[（(]?\d*|有货\s*[（(]?\d*|Haier/海尔|Casarte/卡萨帝", joined, re.I)
    )


def _is_haier_browsing_history_surface(nodes: Sequence[SemanticNode]) -> bool:
    if not _top_visible_title(nodes, re.compile(r"(?:历史浏览|浏览记录)", re.I)):
        return False
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    joined = " | ".join(values[:240])
    return bool(
        re.search(r"商品浏览|线下门店", joined, re.I)
        and re.search(r"今天|管理", joined, re.I)
    )


def _is_haier_checkout_confirmation_surface(
    nodes: Sequence[SemanticNode],
) -> bool:
    values = {
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    return all(
        any(re.fullmatch(pattern, value, re.I) for value in values)
        for pattern in (
            r"权益选择提醒",
            r"此订单存在可选择的权益",
            r"直接提交",
            r"选择权益",
        )
    )


def _is_haier_order_center_surface(nodes: Sequence[SemanticNode]) -> bool:
    values = {
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    order_tabs = {
        pattern
        for pattern in (
            r"全部",
            r"待付款",
            r"待发货",
            r"待收货(?:/验收)?",
            r"待评价",
            r"退款/售后",
        )
        if any(re.fullmatch(pattern, value, re.I) for value in values)
    }
    has_order_content = any(
        re.search(
            r"等待付款|去支付|取消订单|暂无待.{0,6}订单|售后申请|退款详情",
            value,
            re.I,
        )
        for value in values
    )
    return len(order_tabs) >= 4 and has_order_content


def _is_haier_product_grid_surface(nodes: Sequence[SemanticNode]) -> bool:
    """Recognize Haier campaign search results exposed as a compact product grid."""
    page_width = max((node.bounds[2] for node in nodes if node.bounds), default=0)
    page_height = max((node.bounds[3] for node in nodes if node.bounds), default=0)
    if page_width <= 0 or page_height <= 0:
        return False
    cards = [
        node
        for node in nodes
        if _is_haier_product_grid_card(node, (page_width, page_height))
    ]
    if len(cards) < 4:
        return False
    x_centers = sorted((item.bounds[0] + item.bounds[2]) / 2 for item in cards)
    y_centers = sorted((item.bounds[1] + item.bounds[3]) / 2 for item in cards)
    has_multiple_columns = x_centers[-1] - x_centers[0] >= page_width * 0.35
    has_multiple_rows = y_centers[-1] - y_centers[0] >= page_height * 0.12
    return has_multiple_columns and has_multiple_rows


def _is_haier_product_grid_card(
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    if (
        not node.visible
        or not node.enabled
        or not node.clickable
        or node.bounds is None
    ):
        return False
    label = normalize_semantic_text(node.content_desc or node.text)
    width_ratio = (node.bounds[2] - node.bounds[0]) / max(1, screen_size[0])
    height_ratio = (node.bounds[3] - node.bounds[1]) / max(1, screen_size[1])
    return bool(
        0.20 <= width_ratio <= 0.45
        and 0.12 <= height_ratio <= 0.35
        and _MONEY_RE.search(label)
        and _HAIER_APPLIANCE_RE.search(label)
    )


def _top_visible_title(nodes: Sequence[SemanticNode], pattern: re.Pattern[str]) -> bool:
    page_bottom = max(
        (node.bounds[3] for node in nodes if node.visible and node.bounds),
        default=0,
    )
    return any(
        node.visible
        and not node.clickable
        and node.bounds is not None
        and page_bottom > 0
        and node.bounds[1] <= page_bottom * 0.20
        and pattern.fullmatch(normalize_semantic_text(node.content_desc or node.text))
        for node in nodes
    )


def _is_settings_surface(nodes: Sequence[SemanticNode]) -> bool:
    if not _top_visible_title(nodes, re.compile(r"设置", re.I)):
        return False
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    return sum(bool(_SETTINGS_SIGNAL_RE.search(value)) for value in values) >= 2


def _is_address_list_surface(nodes: Sequence[SemanticNode]) -> bool:
    if not _top_visible_title(nodes, re.compile(r"收货地址", re.I)):
        return False
    signal_count = sum(
        1
        for node in nodes
        if node.visible
        and node.clickable
        and node.bounds is not None
        and _ADDRESS_LIST_SIGNAL_RE.search(
            normalize_semantic_text(node.content_desc or node.text)
        )
    )
    return signal_count >= 2


def _is_address_form_surface(nodes: Sequence[SemanticNode]) -> bool:
    if not _top_visible_title(
        nodes,
        re.compile(r"(?:新建|编辑)收货地址", re.I),
    ):
        return False
    values = {
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    return sum(bool(_ADDRESS_FORM_SIGNAL_RE.search(value)) for value in values) >= 3


def _is_invoice_form_surface(nodes: Sequence[SemanticNode]) -> bool:
    if not _top_visible_title(nodes, re.compile(r"(?:添加|编辑)抬头", re.I)):
        return False
    values = {
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    return sum(bool(_INVOICE_FORM_SIGNAL_RE.search(value)) for value in values) >= 2


def _infer_page_role(
    identity_nodes: Sequence[SemanticNode],
    activity: Any,
) -> str:
    visible_nodes = [node for node in identity_nodes if node.visible]
    if _is_filter_panel(visible_nodes):
        return "DIALOG"
    if _is_modal_overlay(visible_nodes):
        return "DIALOG"
    if any(
        _DIALOG_CLASS_RE.search(f"{node.class_name} {node.resource_id}")
        for node in visible_nodes
    ):
        return "DIALOG"
    if _is_permission_decision_dialog(visible_nodes):
        return "DIALOG"
    if any(_OPAQUE_CLASS_RE.search(node.class_name) for node in visible_nodes) and not any(
        node.semantic for node in visible_nodes
    ):
        return "OPAQUE"
    if _is_profile_surface(visible_nodes):
        return "PROFILE"
    if _is_community_feed_surface(visible_nodes):
        return "LIST"
    if _is_settings_surface(visible_nodes):
        return "SETTINGS"
    if _is_address_form_surface(visible_nodes):
        return "SETTINGS"
    if _is_invoice_form_surface(visible_nodes):
        return "SETTINGS"
    if _is_address_list_surface(visible_nodes):
        return "LIST"

    activity_text = _activity_role_text(activity)
    for role, pattern in _PAGE_ROLE_PATTERNS:
        if activity_text and pattern.search(activity_text):
            return role

    page_bottom = max(
        (node.bounds[3] for node in identity_nodes if node.bounds is not None),
        default=0,
    )
    page_right = max(
        (node.bounds[2] for node in identity_nodes if node.bounds is not None),
        default=0,
    )
    repeated_cards = 0
    for node in visible_nodes:
        if not node.clickable or node.bounds is None:
            continue
        width = (node.bounds[2] - node.bounds[0]) / max(1, page_right)
        height = (node.bounds[3] - node.bounds[1]) / max(1, page_bottom)
        # A second card is often only partially visible at the bottom of the
        # viewport.  It is still useful evidence that this is a collection,
        # although the clipped card remains excluded from action discovery by
        # ``_node_has_usable_extent`` below.
        if width < 0.70 or height < 0.05:
            continue
        subtree_values = [
            value
            for candidate in visible_nodes
            if candidate.ordinal == node.ordinal
            or (
                len(candidate.path) > len(node.path)
                and candidate.path[: len(node.path)] == node.path
            )
            for value in (candidate.content_desc, candidate.text)
            if value
        ]
        if any(_REPEATED_CARD_TRANSACTION_RE.search(value) for value in subtree_values):
            repeated_cards += 1
    if repeated_cards >= 2:
        return "LIST"

    scores = {role: 0 for role, _ in _PAGE_ROLE_PATTERNS}
    strong_roles: set[str] = set()
    for node in identity_nodes:
        if not node.visible:
            continue
        values = {
            normalize_semantic_text(node.content_desc),
            normalize_semantic_text(node.text),
        }
        values.discard("")
        for role, pattern in _PAGE_ROLE_PATTERNS:
            matched_values = [
                value for value in values if len(value) <= 120 and pattern.search(value)
            ]
            if not matched_values:
                continue
            # HOME/LIST labels mounted in a bottom navigation bar describe a
            # destination, not necessarily the currently visible page.
            if (
                role in {"HOME", "LIST"}
                and node.bounds is not None
                and page_bottom > 0
                and node.bounds[1] >= page_bottom * 0.78
            ):
                continue
            scores[role] += 2 + int(not node.clickable)
            resource_title = bool(_SAFETY_CONTEXT_RESOURCE_RE.search(node.resource_id))
            top_label = bool(
                node.bounds is not None
                and page_bottom > 0
                and node.bounds[1] <= page_bottom * 0.25
            )
            checkout_cta = bool(
                role == "CHECKOUT"
                and any(
                    re.fullmatch(
                        r"确认订单|提交订单|去结算|订单结算|"
                        r"checkout|place\s*order",
                        value,
                        re.I,
                    )
                    for value in matched_values
                )
                and node.clickable
            )
            if resource_title or top_label or checkout_cta:
                strong_roles.add(role)

    best_role = "UNKNOWN"
    best_score = 0
    for role, _ in _PAGE_ROLE_PATTERNS:
        if scores[role] > best_score:
            best_role = role
            best_score = scores[role]
    if best_role == "CHECKOUT" and best_role not in strong_roles:
        scores[best_role] = 0
        best_role, best_score = max(scores.items(), key=lambda item: item[1])
    return best_role if best_score >= 2 else "UNKNOWN"


def _is_service_detail_surface(nodes: Sequence[SemanticNode]) -> bool:
    visible_values = [
        (node, normalize_semantic_text(value))
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    offering_values = {
        value.casefold()
        for _, value in visible_values
        if _SERVICE_DETAIL_OFFERING_RE.fullmatch(value)
    }
    has_provider_offering = any(
        _SERVICE_DETAIL_PROVIDER_OFFERING_RE.fullmatch(value)
        for _, value in visible_values
    )
    if not offering_values and not has_provider_offering:
        return False

    has_transaction = any(
        node.clickable
        and (
            _product_detail_action_role(value)
            in {"ADD_CART", "BUY_NOW", "ARRIVAL_NOTICE"}
            or _SERVICE_DETAIL_ACTION_RE.fullmatch(value)
        )
        for node, value in visible_values
    )
    if not has_transaction:
        return False

    has_provider = any(
        _SERVICE_DETAIL_PROVIDER_RE.fullmatch(value)
        for _, value in visible_values
    )
    has_selected_offering = any(
        node.clickable
        and re.match(r"^已选\s*[,，:：]", value, re.I)
        and any(offering in value.casefold() for offering in offering_values)
        for node, value in visible_values
    )
    has_service_terms = any(
        _SERVICE_DETAIL_TERMS_RE.search(value) for _, value in visible_values
    )
    has_service_action = any(
        node.clickable and _SERVICE_DETAIL_ACTION_RE.fullmatch(value)
        for node, value in visible_values
    )

    # Service vocabulary also appears in ordinary product descriptions. A
    # service offering therefore needs two independent corroborators, while a
    # provider-qualified title or a service-specific CTA is strong enough by
    # itself. This keeps copy such as "上门服务收费50元" on a goods detail page.
    support_score = (
        int(has_provider)
        + int(has_selected_offering)
        + int(has_service_terms)
        + 2 * int(has_provider_offering or has_service_action)
    )
    return support_score >= 2


def _is_auth_gate_surface(nodes: Sequence[SemanticNode]) -> bool:
    """Recognize Haier's explicit guest boundary without matching profile CTAs."""
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    joined = " | ".join(values[:160])
    if re.search(r"(?:请先登录|登录后(?:购买|查看|继续)|尚未登录|未登录)", joined, re.I):
        return True
    has_login_action = any(
        node.visible
        and node.enabled
        and node.clickable
        and re.fullmatch(
            r"(?:登录|立即登录|登录/注册|验证码登录|密码登录|sign\s*in)",
            normalize_semantic_text(node.content_desc or node.text),
            re.I,
        )
        for node in nodes
    )
    credential_signals = sum(
        bool(re.search(pattern, joined, re.I))
        for pattern in (
            r"手机号|手机号码|phone",
            r"验证码|密码|verification\s*code|password",
            r"用户协议|隐私政策|user\s+agreement|privacy",
        )
    )
    return bool(has_login_action and credential_signals >= 2)


def _infer_page_subtype(
    identity_nodes: Sequence[SemanticNode],
    *,
    role: str,
    package_name: str = "",
) -> str:
    """Classify coverage-relevant list variants without using entry history."""
    is_haier_mall = (
        normalize_semantic_text(package_name).casefold() in _HAIER_MALL_PACKAGES
    )
    if _is_filter_panel(identity_nodes):
        return "FILTER_PANEL"
    if _is_search_surface(identity_nodes) or (
        is_haier_mall and _is_haier_search_query_surface(identity_nodes)
    ):
        return "SEARCH"
    if _is_appointment_list(identity_nodes):
        return "APPOINTMENT_LIST"
    # Cashier safety is stronger than generic overlay presentation. Some
    # cashier implementations expose a full-screen clickable background.
    if _is_cashier_surface(identity_nodes):
        return "CASHIER"
    if _is_auth_gate_surface(identity_nodes):
        return "AUTH_GATE"
    if is_haier_mall and _is_haier_checkout_confirmation_surface(identity_nodes):
        return "CHECKOUT_CONFIRMATION"
    # A bottom sheet keeps the accessibility nodes from the page underneath.
    # Classify the foreground surface first so store/checkout actions behind
    # the scrim cannot leak into the dialog action set.
    if _is_modal_overlay(identity_nodes):
        return (
            "PURCHASE_OPTIONS"
            if _is_purchase_options_panel(identity_nodes)
            else "MODAL_PANEL"
        )
    if role == "DIALOG" and _is_permission_decision_dialog(identity_nodes):
        return "MODAL_PANEL"
    if role == "PRODUCT_DETAIL" and _is_service_detail_surface(identity_nodes):
        return "SERVICE_DETAIL"
    if _is_store_detail(identity_nodes):
        return "STORE_DETAIL"
    if is_haier_mall:
        if _is_haier_search_results_surface(identity_nodes):
            return "PRODUCT_LIST"
        if _is_haier_community_detail_surface(identity_nodes):
            return "COMMUNITY_DETAIL"
        if _is_haier_member_benefits_surface(identity_nodes):
            return "MEMBER_BENEFITS"
        if _is_haier_order_center_surface(identity_nodes):
            return "ORDER"
        if _is_haier_favorites_surface(identity_nodes):
            return "FAVORITES"
        if _is_haier_browsing_history_surface(identity_nodes):
            return "BROWSING_HISTORY"
        if role == "UNKNOWN" and _is_haier_product_grid_surface(identity_nodes):
            return "PRODUCT_LIST"
    values = [
        normalize_semantic_text(value)
        for node in identity_nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    joined = " | ".join(values[:160])
    if role == "LIST" and _is_cart_surface(identity_nodes):
        return "CART"
    if role == "LIST" and _is_community_feed_surface(identity_nodes):
        return "COMMUNITY_FEED"
    if role == "LIST" and _is_address_list_surface(identity_nodes):
        return "ADDRESS_LIST"
    if role == "SETTINGS" and _is_address_form_surface(identity_nodes):
        return "ADDRESS_FORM"
    if role == "SETTINGS" and _is_invoice_form_surface(identity_nodes):
        return "INVOICE_FORM"
    if role == "LIST" and _catalog_sidebar_members(identity_nodes):
        # The mall's bottom-level "分类" page is a two-pane catalog: a long
        # category rail on the left and image entries on the right. It has no
        # sort/filter row, so treating it as a generic PRODUCT_LIST makes every
        # rail item a separate command and repeatedly re-explores the same
        # structure.
        return "CATALOG_CATEGORY"
    store_signals = sum(
        1
        for pattern in (
            r"立即预约",
            r"门店电话",
            r"进店逛逛|visit\s+store",
            r"\d+(?:\.\d+)?\s*km",
            r"附近门店|线下专卖店",
        )
        if re.search(pattern, joined, re.I)
    )
    page_bottom = max(
        (node.bounds[3] for node in identity_nodes if node.bounds is not None),
        default=0,
    )
    has_store_page_title = any(
        node.visible
        and not node.clickable
        and node.bounds is not None
        and page_bottom > 0
        and node.bounds[1] <= int(page_bottom * 0.20)
        and re.fullmatch(
            r"附近门店|线下专卖店|nearby\s+stores?",
            normalize_semantic_text(node.content_desc or node.text),
            re.I,
        )
        for node in identity_nodes
    )
    if store_signals >= 2 and (role == "LIST" or has_store_page_title):
        return "STORE_LIST"
    if role == "LIST":
        sort_labels = {
            value.casefold()
            for value in values
            if value.casefold() in _SORT_LABELS
        }
        has_filter = any(_semantic_kind(value) == "FILTER" for value in values)
        if len(sort_labels) >= 2 and has_filter:
            return "CATALOG_CATEGORY"
        if len(_SERVICE_LIST_RE.findall(joined)) >= 2:
            return "SERVICE_LIST"
        if len(_CONSUMABLE_LIST_RE.findall(joined)) >= 2:
            return "CONSUMABLE_LIST"
        if _PRODUCT_LIST_RE.search(joined):
            return "PRODUCT_LIST"
        return "PRODUCT_LIST"
    if role in {
        "HOME",
        "PRODUCT_DETAIL",
        "CHECKOUT",
        "ORDER",
        "DIALOG",
        "OPAQUE",
        "PROFILE",
        "SETTINGS",
        "ADDRESS_LIST",
    }:
        return role
    return "UNKNOWN"


def _catalog_sidebar_members(
    nodes: Sequence[SemanticNode],
) -> List[SemanticNode]:
    """Return the compact left-hand category rail when one is present."""
    page_width = max((node.bounds[2] for node in nodes if node.bounds), default=0)
    page_height = max((node.bounds[3] for node in nodes if node.bounds), default=0)
    if page_width <= 0 or page_height <= 0:
        return []

    by_parent: Dict[Optional[int], List[SemanticNode]] = {}
    for node in nodes:
        if not (
            node.visible
            and node.enabled
            and node.clickable
            and node.bounds is not None
        ):
            continue
        left, top, right, bottom = node.bounds
        label = normalize_semantic_text(node.content_desc or node.text)
        width = (right - left) / page_width
        height = (bottom - top) / page_height
        if not (
            left / page_width <= 0.03
            and right / page_width <= 0.30
            and top / page_height >= 0.07
            and bottom / page_height <= 0.95
            and width >= 0.12
            and 0.035 <= height <= 0.10
            and 1 <= len(label) <= 12
        ):
            continue
        by_parent.setdefault(node.parent_ordinal, []).append(node)

    groups = [items for items in by_parent.values() if len(items) >= 5]
    if not groups:
        return []
    members = max(groups, key=len)
    widths = [node.bounds[2] - node.bounds[0] for node in members if node.bounds]
    heights = [node.bounds[3] - node.bounds[1] for node in members if node.bounds]
    if (
        not widths
        or not heights
        or min(widths) / max(widths) < 0.80
        or min(heights) / max(heights) < 0.70
    ):
        return []

    # Require a real content pane to the right so a generic drawer/menu is not
    # mistaken for the mall category surface. Some selected categories render
    # the pane through a canvas-like ViewGroup and expose no semantic children.
    has_content_pane = any(
        node.visible
        and node.bounds is not None
        and node.bounds[0] / page_width >= 0.20
        and (node.bounds[2] - node.bounds[0]) / page_width >= 0.50
        and node.bounds[1] / page_height >= 0.07
        and node.bounds[3] / page_height <= 0.95
        and (node.bounds[3] - node.bounds[1]) / page_height >= 0.45
        for node in nodes
    )
    right_entries = [
        node
        for node in nodes
        if node.visible
        and node.enabled
        and node.clickable
        and node.bounds is not None
        and node.bounds[0] / page_width >= 0.20
        and (node.bounds[2] - node.bounds[0]) / page_width >= 0.12
        and (node.bounds[3] - node.bounds[1]) / page_height >= 0.05
        and node.bounds[3] / page_height <= 0.90
    ]
    if not has_content_pane and len(right_entries) < 2:
        return []
    return sorted(members, key=lambda item: item.bounds or (0, 0, 0, 0))


def _is_permission_decision_dialog(
    nodes: Sequence[SemanticNode],
) -> bool:
    values = [
        normalize_semantic_text(value)
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    has_request = any(
        re.search(
            r"申请.*权限|定位权限|permission\s+request",
            value,
            re.I,
        )
        for value in values
    )
    has_decision = any(
        re.search(
            r"暂不开启|去开启|允许|拒绝|取消|allow|deny|cancel",
            value,
            re.I,
        )
        for value in values
    )
    return has_request and has_decision


def _is_cart_surface(nodes: Sequence[SemanticNode]) -> bool:
    """Recognize a cart title without confusing bottom navigation labels."""
    visible = [node for node in nodes if node.visible and node.bounds is not None]
    page_bottom = max((node.bounds[3] for node in visible), default=0)
    if page_bottom <= 0:
        return False
    return any(
        not node.clickable
        and node.bounds is not None
        and node.bounds[1] <= int(page_bottom * 0.20)
        and node.bounds[3] <= int(page_bottom * 0.30)
        and re.fullmatch(
            r"购物车|shopping\s+cart|cart",
            normalize_semantic_text(node.content_desc or node.text),
            re.I,
        )
        for node in visible
    )


def _is_cashier_surface(nodes: Sequence[SemanticNode]) -> bool:
    """Recognize a top-anchored Haier cashier without matching body copy."""
    visible = [node for node in nodes if node.visible and node.bounds is not None]
    page_bottom = max((node.bounds[3] for node in visible), default=0)
    if page_bottom <= 0:
        return False
    return any(
        not node.clickable
        and node.bounds is not None
        and node.bounds[1] <= int(page_bottom * 0.18)
        and node.bounds[3] <= int(page_bottom * 0.30)
        and any(
            _HAIER_CASHIER_ANCHOR_RE.fullmatch(_normalize_safety_text(value))
            for value in (node.content_desc, node.text)
            if value
        )
        for node in visible
    )


def _is_modal_overlay(nodes: Sequence[SemanticNode]) -> bool:
    """Detect an accessibility-exposed scrim with a foreground sheet."""
    visible = [node for node in nodes if node.visible and node.bounds is not None]
    page_right = max((node.bounds[2] for node in visible), default=0)
    page_bottom = max((node.bounds[3] for node in visible), default=0)
    if page_right <= 0 or page_bottom <= 0:
        return False
    scrims = [
        node
        for node in visible
        if node.clickable
        and not normalize_semantic_text(node.content_desc or node.text)
        and node.bounds[0] <= int(page_right * 0.02)
        and node.bounds[1] <= int(page_bottom * 0.03)
        and node.bounds[2] >= int(page_right * 0.98)
        and node.bounds[3] >= int(page_bottom * 0.95)
    ]
    if not scrims:
        return False
    panels = [
        node
        for node in visible
        if _class_family(node.class_name) == "container"
        and node.bounds[0] <= int(page_right * 0.08)
        and node.bounds[2] >= int(page_right * 0.92)
        and int(page_bottom * 0.15) <= node.bounds[1] <= int(page_bottom * 0.75)
        and node.bounds[3] >= int(page_bottom * 0.90)
    ]
    return any(
        not (
            len(panel.path) >= len(scrim.path)
            and panel.path[: len(scrim.path)] == scrim.path
        )
        for scrim in scrims
        for panel in panels
        if panel.ordinal != scrim.ordinal
    )


def _is_filter_panel(nodes: Sequence[SemanticNode]) -> bool:
    """Recognize a faceted filter sheet even when the app exposes no dialog class."""
    values = {
        normalize_semantic_text(value).casefold()
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    facet_count = len(values.intersection(_FILTER_PANEL_FACETS))
    has_explicit_title = "全部筛选" in values or "all filters" in values
    has_generic_title = "筛选" in values or "filter" in values
    has_reset = bool(values.intersection({"重置", "reset"}))
    has_confirm = bool(values.intersection({"确定", "确认", "应用", "apply"}))
    # Accessibility trees can omit one of the two bottom buttons while the
    # sheet is animating or partially occluded.  "全部筛选" plus two facet names
    # is already specific enough; a generic title keeps the stricter gates.
    if has_explicit_title:
        return bool(facet_count >= 2 and (has_reset or has_confirm))
    return bool(
        has_generic_title
        and facet_count >= 3
        and has_reset
        and has_confirm
    )


def _is_purchase_options_panel(nodes: Sequence[SemanticNode]) -> bool:
    """Recognize a product/SKU chooser without enumerating every variant."""
    visible = [node for node in nodes if node.visible]
    values = [
        normalize_semantic_text(value)
        for node in visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    ]
    has_primary_cta = any(
        node.clickable
        and any(
            re.fullmatch(
                r"立即购买|加入购物车|buy\s+now|add\s+to\s+cart",
                value,
                re.I,
            )
            for value in (
                normalize_semantic_text(node.content_desc),
                normalize_semantic_text(node.text),
            )
            if value
        )
        for node in visible
    )
    has_option_context = any(
        re.search(
            r"(?:^|[,，\s])(?:已选|规格|型号|颜色|尺寸|数量|库存|选择规格)"
            r"|(?:selected|specification|variant|colour|color|size|stock|quantity)",
            value,
            re.I,
        )
        for value in values
    )
    return bool(has_primary_cta and has_option_context)


def _is_search_surface(nodes: Sequence[SemanticNode]) -> bool:
    values = {
        normalize_semantic_text(value).casefold()
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    has_search_input = any(
        node.visible and node.editable for node in nodes
    )
    return bool(
        has_search_input
        and values.intersection({"搜索", "search"})
        and values.intersection(
            {
                "搜索历史",
                "最近搜索",
                "热门搜索",
                "history",
                "recent searches",
                "popular searches",
            }
        )
    )


def _is_haier_search_query_surface(nodes: Sequence[SemanticNode]) -> bool:
    """Keep Haier's populated suggestion surface in the SEARCH state family."""
    has_search_input = any(
        node.visible and node.enabled and node.editable for node in nodes
    )
    has_submit = any(
        node.visible
        and node.enabled
        and node.clickable
        and re.fullmatch(
            r"(?:搜索|search)",
            normalize_semantic_text(node.content_desc or node.text),
            re.I,
        )
        for node in nodes
    )
    return bool(has_search_input and has_submit)


def _is_haier_search_results_surface(nodes: Sequence[SemanticNode]) -> bool:
    """Distinguish Haier keyword results from the bottom-tab category page."""
    page_width = max(
        (node.bounds[2] for node in nodes if node.visible and node.bounds),
        default=0,
    )
    page_height = max(
        (node.bounds[3] for node in nodes if node.visible and node.bounds),
        default=0,
    )
    if page_width <= 0 or page_height <= 0:
        return False
    if any(node.visible and node.editable for node in nodes):
        return False
    values = {
        normalize_semantic_text(value).casefold()
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    sort_labels = values.intersection(set(_SORT_LABELS))
    has_filter = bool(values.intersection({"筛选", "filter"}))
    has_query_header = any(
        node.visible
        and node.clickable
        and node.bounds is not None
        and node.bounds[1] <= page_height * 0.12
        and (node.bounds[2] - node.bounds[0]) >= page_width * 0.45
        and _HAIER_APPLIANCE_RE.search(
            normalize_semantic_text(node.content_desc or node.text)
        )
        for node in nodes
    )
    price_count = sum(
        1
        for node in nodes
        if node.visible
        and any(
            _MONEY_RE.search(normalize_semantic_text(value))
            for value in (node.content_desc, node.text)
            if normalize_semantic_text(value)
        )
    )
    return bool(
        len(sort_labels) >= 2
        and has_filter
        and has_query_header
        and price_count >= 2
    )


def _is_appointment_list(nodes: Sequence[SemanticNode]) -> bool:
    values = {
        normalize_semantic_text(value).casefold()
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    return bool(
        values.intersection({"取消预约", "cancel booking"})
        and values.intersection(
            {"我的预约", "预约时间", "已预约", "my bookings", "booking time"}
        )
    )


def _is_store_detail(nodes: Sequence[SemanticNode]) -> bool:
    values = {
        normalize_semantic_text(value).casefold()
        for node in nodes
        if node.visible
        for value in (node.content_desc, node.text)
        if normalize_semantic_text(value)
    }
    joined = " | ".join(sorted(values))
    return bool(
        values.intersection({"切换门店", "switch store"})
        and values.intersection({"预约", "电话", "book", "call"})
        and re.search(r"距(?:您)?\s*\d+(?:\.\d+)?\s*km|\d+(?:\.\d+)?\s*km", joined, re.I)
    )


def _class_family(class_name: Any) -> str:
    value = normalize_semantic_text(class_name).casefold()
    simple = value.rsplit(".", 1)[-1]
    if any(token in simple for token in ("edit", "input", "textfield")):
        return "input"
    if any(token in simple for token in ("checkbox", "radiobutton", "switch", "toggle")):
        return "toggle"
    if "button" in simple:
        return "button"
    if any(token in simple for token in ("recycler", "listview", "gridview", "collection")):
        return "collection"
    if "scroll" in simple:
        return "scroll"
    if any(token in simple for token in ("image", "icon")):
        return "image"
    if "text" in simple or simple in {"label", "statictext"}:
        return "text"
    if any(token in simple for token in ("webview", "surface", "canvas", "mapview")):
        return "opaque"
    if any(
        token in simple
        for token in (
            "layout",
            "viewgroup",
            "container",
            "composeview",
            "contentview",
        )
    ) or simple in {"view", "node", "hierarchy"}:
        return "container"
    return simple or "unknown"


def _semantic_kind(value: Any) -> str:
    text = normalize_semantic_text(value)
    if not text:
        return ""
    for kind, pattern in _SEMANTIC_KIND_PATTERNS:
        if pattern.search(text):
            return kind
    for role, pattern in _PAGE_ROLE_PATTERNS:
        if pattern.search(text):
            return f"ROLE_{role}"
    return ""


def _default_action_risk(value: Any, role: str) -> str:
    text = _normalize_safety_text(value)
    if not text:
        return ""
    for risk_type, pattern in _DEFAULT_BLOCK_RULES:
        if re.search(pattern, text, re.I):
            return risk_type
    # This token contributes only to structural risk signatures. Runtime
    # blocking additionally requires a verified Haier cashier page.
    if _is_final_payment_action(text):
        return "PAYMENT"
    return ""


def _local_parent(
    node: SemanticNode,
    by_ordinal: Dict[int, SemanticNode],
) -> Optional[SemanticNode]:
    parent = by_ordinal.get(node.parent_ordinal) if node.parent_ordinal is not None else None
    return parent


def _local_depth(
    node: SemanticNode,
    by_ordinal: Dict[int, SemanticNode],
) -> int:
    depth = 0
    current = _local_parent(node, by_ordinal)
    while current is not None:
        depth += 1
        current = _local_parent(current, by_ordinal)
    return depth


def _structure_payload(
    node: SemanticNode,
    *,
    by_ordinal: Dict[int, SemanticNode],
    child_families: Dict[int, List[str]],
) -> Dict[str, Any]:
    parent = _local_parent(node, by_ordinal)
    return {
        "class": _class_family(node.class_name),
        "parent_class": _class_family(parent.class_name) if parent is not None else "root",
        "depth": _local_depth(node, by_ordinal),
        "children": sorted(set(child_families.get(node.ordinal, ()))),
        "clickable": node.clickable,
        "editable": node.editable,
        "scrollable": node.scrollable,
        "password": node.password,
    }


def _page_screen_size(
    page: PageModel,
    screen_size: Optional[Tuple[int, int]] = None,
) -> Tuple[int, int]:
    if screen_size and screen_size[0] > 0 and screen_size[1] > 0:
        return int(screen_size[0]), int(screen_size[1])
    width = max((node.bounds[2] for node in page.nodes if node.bounds), default=1)
    height = max((node.bounds[3] for node in page.nodes if node.bounds), default=1)
    return max(1, width), max(1, height)


def _node_has_usable_extent(
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    """Reject clipped slivers that cannot be intentional touch targets."""
    if node.bounds is None:
        return True
    screen_width, screen_height = screen_size
    x1, y1, x2, y2 = node.bounds
    visible_width = max(0, min(screen_width, x2) - max(0, x1))
    visible_height = max(0, min(screen_height, y2) - max(0, y1))
    normalized_width = visible_width / max(1, screen_width)
    normalized_height = visible_height / max(1, screen_height)
    if (
        (node.clickable or node.editable or node.scrollable)
        and y2 >= screen_height * 0.98
        and normalized_height < 0.08
    ):
        return False
    return bool(
        normalized_width >= _MIN_NORMALIZED_ACTION_DIMENSION
        and normalized_height >= _MIN_NORMALIZED_ACTION_DIMENSION
    )


def _scroll_orientation(node: SemanticNode) -> str:
    return "horizontal" if "horizontal" in node.class_name.casefold() else "vertical"


def _scroll_directions(node: SemanticNode) -> Tuple[str, str]:
    return ("left", "right") if _scroll_orientation(node) == "horizontal" else ("up", "down")


def _bounds_area(bounds: Optional[Tuple[int, int, int, int]]) -> int:
    if bounds is None:
        return 0
    x1, y1, x2, y2 = bounds
    return max(0, x2 - x1) * max(0, y2 - y1)


def _bounds_intersection_area(
    left: Optional[Tuple[int, int, int, int]],
    right: Optional[Tuple[int, int, int, int]],
) -> int:
    if left is None or right is None:
        return 0
    return max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _same_scroll_region(left: SemanticNode, right: SemanticNode) -> bool:
    if _scroll_orientation(left) != _scroll_orientation(right):
        return False
    left_area = _bounds_area(left.bounds)
    right_area = _bounds_area(right.bounds)
    intersection = _bounds_intersection_area(left.bounds, right.bounds)
    if not left_area or not right_area or not intersection:
        return False
    union = left_area + right_area - intersection
    return bool(
        intersection / min(left_area, right_area) >= 0.90
        and intersection / max(1, union) >= 0.80
    )


def _collapse_overlapping_scroll_nodes(
    nodes: Sequence[SemanticNode],
) -> List[SemanticNode]:
    """Keep one physical swipe target for nested views covering the same region."""
    preferred = sorted(
        (node for node in nodes if node.scrollable),
        key=lambda node: (
            bool(node.stable_desc or node.stable_text),
            len(node.path),
            -_bounds_area(node.bounds),
            -node.ordinal,
        ),
        reverse=True,
    )
    selected: List[SemanticNode] = []
    for node in preferred:
        if any(_same_scroll_region(node, existing) for existing in selected):
            continue
        selected.append(node)
    return sorted(selected, key=lambda node: node.path)


def _normalized_region(
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> str:
    if node.bounds is None:
        return "unknown"
    screen_width, screen_height = screen_size
    x1, y1, x2, y2 = node.bounds
    center_x = (x1 + x2) / 2 / max(1, screen_width)
    center_y = (y1 + y2) / 2 / max(1, screen_height)
    column = min(2, max(0, int(center_x * 3)))
    if center_y < 0.20:
        band = "header"
    elif center_y < 0.34:
        band = "toolbar"
    elif center_y >= 0.84:
        band = "bottom"
    else:
        band = "content"
    return f"{band}:c{column}"


def _product_detail_action_role(label: str) -> Optional[str]:
    for role, pattern in _PRODUCT_DETAIL_ACTION_PATTERNS:
        if pattern.search(label):
            return role
    return None


def _family_identity_role(page_role: str, action_role: str) -> Optional[str]:
    if action_role.startswith(_INSTANCE_ACTION_ROLE_PREFIX):
        return None
    if page_role != "PRODUCT_DETAIL":
        return action_role
    if action_role.startswith("SCROLL:"):
        return "SCROLL_CONTAINER"
    if action_role in _PRODUCT_DETAIL_FAMILY_IDENTITY_ROLES:
        return action_role
    return None


def _is_horizontal_tab_member(
    page: PageModel,
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    """Recognize compact top tabs inside a horizontal scroll container."""
    if not node.clickable or node.bounds is None:
        return False
    screen_width, screen_height = screen_size
    if screen_width <= 0 or screen_height <= 0:
        return False
    _, top, _, bottom = node.bounds
    height = bottom - top
    if (
        top < int(screen_height * 0.05)
        or bottom > int(screen_height * 0.30)
        or height < int(screen_height * 0.025)
        or height > int(screen_height * 0.12)
    ):
        return False

    parent = page.node(node.parent_ordinal)
    if parent is None:
        return False
    ancestor = parent
    horizontal_scroll = False
    for _ in range(3):
        if ancestor.scrollable and _scroll_orientation(ancestor) == "horizontal":
            horizontal_scroll = True
            break
        ancestor = page.node(ancestor.parent_ordinal)
        if ancestor is None:
            break
    if not horizontal_scroll:
        return False

    peers = [
        item
        for item in page.nodes
        if item.parent_ordinal == node.parent_ordinal
        and item.visible
        and item.enabled
        and item.clickable
        and item.bounds is not None
    ]
    if not 4 <= len(peers) <= 12:
        return False
    labels = [
        re.sub(
            r"[,，]?\s*\d+$",
            "",
            normalize_semantic_text(item.content_desc or item.text),
        ).strip()
        for item in peers
    ]
    if any(not label or len(label) > 12 for label in labels):
        return False
    widths = [item.bounds[2] - item.bounds[0] for item in peers]
    heights = [item.bounds[3] - item.bounds[1] for item in peers]
    centers_y = [(item.bounds[1] + item.bounds[3]) / 2 for item in peers]
    span = max(item.bounds[2] for item in peers) - min(
        item.bounds[0] for item in peers
    )
    return bool(
        min(widths) / max(widths) >= 0.50
        and min(heights) / max(heights) >= 0.70
        and max(centers_y) - min(centers_y) <= screen_height * 0.04
        and span >= screen_width * 0.55
    )


def _is_haier_catalog_search_entry(
    page: PageModel,
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    """Recognize the Haier category header search box despite its hot-word text."""
    if (
        normalize_semantic_text(page.package_name).casefold()
        not in _HAIER_MALL_PACKAGES
        or page.page_subtype != "CATALOG_CATEGORY"
        or not node.clickable
        or node.bounds is None
    ):
        return False
    screen_width, screen_height = screen_size
    if screen_width <= 0 or screen_height <= 0:
        return False
    left, top, right, bottom = node.bounds
    width = (right - left) / screen_width
    height = (bottom - top) / screen_height
    return bool(
        left / screen_width <= 0.10
        and right / screen_width >= 0.80
        and top / screen_height <= 0.08
        and bottom / screen_height <= 0.12
        and width >= 0.70
        and 0.025 <= height <= 0.08
    )


def _is_haier_price_purchase_cta(
    page: PageModel,
    node: SemanticNode,
    screen_size: Tuple[int, int],
    label: str,
) -> bool:
    """Recognize Haier's price-labelled buy button in the bottom action bar."""
    raw_label = normalize_semantic_text(node.content_desc or node.text or label)
    if (
        normalize_semantic_text(page.package_name).casefold()
        not in _HAIER_MALL_PACKAGES
        or (
            page.role != "PRODUCT_DETAIL"
            and page.page_subtype != "PURCHASE_OPTIONS"
        )
        or not node.clickable
        or node.bounds is None
        or not re.match(r"^到手价(?:\s*[,，])?", raw_label, re.I)
        or not _MONEY_RE.search(raw_label)
    ):
        return False
    screen_width, screen_height = screen_size
    if screen_width <= 0 or screen_height <= 0:
        return False
    left, top, right, bottom = node.bounds
    in_bottom_bar = bool(
        top / screen_height >= 0.82
        and bottom / screen_height >= 0.90
        and right / screen_width >= 0.90
    )
    if not in_bottom_bar:
        return False
    return bool(
        page.page_subtype == "PURCHASE_OPTIONS"
        or left / screen_width >= 0.55
    )


def _action_role_for_node(
    page: PageModel,
    node: SemanticNode,
    *,
    action_type: str,
    semantic: str,
    screen_size: Tuple[int, int],
    semantic_description: str = "",
    semantic_text: str = "",
    navigation: Optional[Dict[str, Any]] = None,
    direction: Optional[str] = None,
) -> Tuple[str, str, str]:
    label = normalize_semantic_text(semantic).casefold()
    primary_label = _stable_primary_action_label(
        text=semantic_text,
        content_desc=semantic_description,
        fallback=semantic,
    ).casefold()
    region = _normalized_region(node, screen_size)
    class_family = _class_family(node.class_name)
    product_role = (
        _product_detail_action_role(label)
        or (
            "BUY_NOW"
            if _is_haier_price_purchase_cta(
                page,
                node,
                screen_size,
                label,
            )
            else None
        )
        if (
            page.role == "PRODUCT_DETAIL"
            or page.page_subtype == "PURCHASE_OPTIONS"
        )
        and action_type == "click"
        else None
    )
    if action_type == "scroll":
        orientation = _scroll_orientation(node)
        role = f"SCROLL:{orientation}:{str(direction or 'up').lower()}"
    elif node.editable or action_type == "input":
        role = "INPUT"
    elif class_family == "toggle":
        role = "TOGGLE"
    elif label in _SORT_LABELS:
        role = f"SORT:{_SORT_LABELS[label]}"
    elif _semantic_kind(label) == "FILTER":
        role = "FILTER_OPEN"
    elif product_role:
        role = product_role
    elif (
        page.role == "CHECKOUT"
        and action_type == "click"
        and _is_final_payment_action(primary_label or label)
    ):
        # The mall labels the order-submission CTA as "立即支付" even though
        # it only opens the cashier.  Treat it as the high-value checkout
        # transition here; runtime payment blocking remains scoped to a
        # strongly anchored Haier cashier page in ``classify_risk``.
        role = "PLACE_ORDER"
    elif (
        page.page_subtype == "CHECKOUT_CONFIRMATION"
        and action_type == "click"
        and re.fullmatch(r"直接提交", primary_label or label, re.I)
    ):
        role = "PLACE_ORDER"
    elif page.page_subtype == "CHECKOUT_CONFIRMATION" and action_type == "click":
        role = "DIALOG_OPTION"
    elif page.page_subtype == "SEARCH" and action_type == "click":
        semantic_kind = _semantic_kind(primary_label or label)
        role = (
            "SEARCH_SUBMIT"
            if semantic_kind == "SEARCH"
            else "SEARCH_MODE"
            if normalize_semantic_text(primary_label or label).casefold()
            in {"商品", "服务", "product", "service"}
            else "SEARCH_SUGGESTION"
        )
    elif page.page_subtype in {"STORE_LIST", "STORE_DETAIL"} and re.search(
        r"门店电话|电话|拨打|call|phone", primary_label or label, re.I
    ):
        role = "STORE_CALL"
    elif page.page_subtype == "STORE_DETAIL" and re.search(
        r"我的预约|预约记录|my\s+bookings?", primary_label or label, re.I
    ):
        role = "STORE_BOOKINGS"
    elif page.page_subtype == "STORE_DETAIL" and re.fullmatch(
        r"选品|店内商品|products?", primary_label or label, re.I
    ):
        role = "STORE_PRODUCTS"
    elif page.page_subtype in {"STORE_LIST", "STORE_DETAIL"} and re.search(
        r"预约|book", primary_label or label, re.I
    ):
        role = "STORE_APPOINTMENT"
    elif page.page_subtype == "STORE_LIST" and _is_collection_card(
        node, screen_size
    ):
        role = "STORE_OPEN"
    elif navigation:
        destination = _hash_payload(
            {"navigation": primary_label or label or navigation.get("member_key")}
        )
        role = f"NAV:{destination}"
    elif action_type == "click" and _is_haier_catalog_search_entry(
        page,
        node,
        screen_size,
    ):
        role = "COMMAND:SEARCH"
    elif (
        action_type == "click"
        and normalize_semantic_text(page.package_name).casefold()
        in _HAIER_MALL_PACKAGES
        and page.page_subtype == "PRODUCT_LIST"
        and _is_haier_product_grid_card(node, screen_size)
    ):
        role = "ITEM_OPEN:collection"
    elif action_type == "click" and _is_horizontal_tab_member(
        page,
        node,
        screen_size,
    ):
        role = "CATEGORY_TAB:top"
    elif page.role == "LIST" and _is_collection_card(node, screen_size):
        # A product card can start in the first viewport band. Classifying by
        # vertical position before card geometry turns its title or feature
        # copy into a category tab and creates a false page family.
        role = "ITEM_OPEN:collection"
    elif (
        page.page_subtype == "CATALOG_CATEGORY"
        and _is_catalog_sidebar_member(page, node)
    ):
        role = "CATEGORY_TAB:side"
    elif _is_catalog_grid_entry(page, node, screen_size):
        role = "ITEM_OPEN:collection"
    elif (
        page.role == "LIST"
        and node.bounds is not None
        and node.bounds[3] / max(1, screen_size[1]) <= 0.28
    ):
        role = "CATEGORY_TAB:top"
    elif page.role == "LIST" and node.bounds is not None:
        width = (node.bounds[2] - node.bounds[0]) / max(1, screen_size[0])
        height = (node.bounds[3] - node.bounds[1]) / max(1, screen_size[1])
        if width >= 0.70 and height >= 0.08:
            role = "ITEM_OPEN:collection"
        else:
            semantic_kind = _semantic_kind(primary_label or label)
            role = (
                f"COMMAND:{semantic_kind}"
                if semantic_kind
                else f"COMMAND:{_hash_payload({'label': primary_label or label, 'class': class_family})}"
            )
    elif page.role == "PRODUCT_DETAIL":
        role = _INSTANCE_ACTION_ROLE_PREFIX + _hash_payload(
            {"label": label, "class": class_family, "region": region}
        )
    else:
        semantic_kind = _semantic_kind(primary_label or label)
        role = (
            f"COMMAND:{semantic_kind}"
            if semantic_kind
            else f"COMMAND:{_hash_payload({'label': primary_label or label, 'class': class_family})}"
        )
    reusable_across_viewports = bool(
        role.startswith(("ITEM_OPEN:", "CATEGORY_TAB:", "SORT:", "SCROLL:", "NAV:"))
        or role in {"FILTER_OPEN", "INPUT", "TOGGLE"}
    )
    anchor_payload = {"role": role, "class": class_family}
    if not reusable_across_viewports:
        anchor_payload["region"] = region
    anchor = _hash_payload(anchor_payload)
    return role, _hash_payload({"action_role": role}), anchor


def _coverage_action_identity(
    page: PageModel,
    node: SemanticNode,
    *,
    action_role: str,
    risk_type: Optional[str],
    screen_size: Tuple[int, int],
    navigation: Optional[Dict[str, Any]],
    visual_evidence: Optional[Dict[str, Any]],
    semantic: str,
) -> Tuple[str, str, str]:
    """Return group key, concrete instance key and scheduler sample policy."""
    role = str(action_role or "COMMAND:UNKNOWN")
    if role.startswith("NAV:") and navigation:
        group_payload: Dict[str, Any] = {
            "scope": "NAVIGATION",
            "navigation_group": navigation.get("group_key"),
            "destination": navigation.get("member_key") or role,
        }
        policy = "RUN_NAV_ONCE"
    elif visual_evidence:
        group_payload = {
            "scope": "HOME_VISUAL",
            "page_subtype": page.page_subtype,
            "region": _normalized_region(node, screen_size),
            "crop_phash": visual_evidence.get("crop_phash"),
        }
        policy = "HOME_VISUAL"
    else:
        card_layout = "none"
        if _is_collection_card(node, screen_size):
            width = (node.bounds[2] - node.bounds[0]) / max(1, screen_size[0])
            card_layout = "full" if width >= 0.90 else "wide"
        group_page_subtype = page.page_subtype
        if role.startswith("ITEM_OPEN:") and page.page_subtype in {
            "CONSUMABLE_LIST",
            "PRODUCT_LIST",
            "SERVICE_LIST",
        }:
            # A long special-list page can be classified differently after a
            # scroll as its visible copy changes. Keep one item group for the
            # owning entry so the viewport never earns a second product click.
            group_page_subtype = "SPECIAL_LIST"
        group_payload = {
            "scope": "FAMILY_ACTION",
            "page_subtype": group_page_subtype,
            "action_role": role,
            "card_layout": card_layout,
            "risk": risk_type or "SAFE",
            "enabled": node.enabled,
            "checked": node.checked,
            "selected": node.selected,
        }
        if role.startswith("ITEM_OPEN:"):
            policy = (
                "FAMILY_TWO_SAMPLES"
                if page.page_subtype == "CATALOG_CATEGORY"
                else "PAGE_ONE"
            )
        elif role == "FILTER_OPEN":
            policy = "PAGE_ONE"
        elif role == "SEARCH_SUGGESTION":
            policy = "PAGE_ONE"
        elif role.startswith("SORT:"):
            policy = "PAGE_ONE"
        elif role.startswith("SCROLL:"):
            policy = "COVERAGE_SCROLL"
        elif role.startswith("CATEGORY_TAB:"):
            policy = "FAMILY_ONE"
        elif role in {
            "STORE_OPEN",
            "STORE_APPOINTMENT",
            "STORE_BOOKINGS",
            "STORE_CALL",
            "STORE_PRODUCTS",
        }:
            policy = "PAGE_ONE"
        elif role.startswith("COMMAND:"):
            # Repeated generic controls with the same semantic intent are one
            # coverage group. Try one representative and at most one fallback
            # candidate instead of paying the locator timeout for every copy.
            policy = "PAGE_ONE"
        else:
            policy = "ALL"
    instance_payload = {
        "group": group_payload,
        "label": normalize_semantic_text(semantic).casefold(),
        "bounds": list(node.bounds or ()),
    }
    return (
        _hash_payload(group_payload),
        _hash_payload(instance_payload),
        policy,
    )


def _family_signature(
    page: PageModel,
    *,
    screen_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    resolved_size = _page_screen_size(page, screen_size)
    catalog_hub = bool(
        page.page_subtype == "CATALOG_CATEGORY"
        and _catalog_sidebar_members(page.nodes)
    )
    visible_nodes = [
        node
        for node in _identity_nodes(page.nodes, page.package_name)
        if node.visible and _node_has_usable_extent(node, resolved_size)
    ]
    visible_by_ordinal = {node.ordinal: node for node in visible_nodes}
    child_families: Dict[int, List[str]] = {}
    for node in visible_nodes:
        if node.parent_ordinal in visible_by_ordinal:
            child_families.setdefault(node.parent_ordinal, []).append(
                _class_family(node.class_name)
            )
    structure_tokens = sorted(
        {
            _hash_payload(
                {
                    "slot": {
                        "class": _class_family(node.class_name),
                        "clickable": node.clickable,
                        "editable": node.editable,
                        "scrollable": node.scrollable,
                        "password": node.password,
                    }
                }
            )
            for node in visible_nodes
        }
    )
    navigation_by_ordinal: Dict[int, Dict[str, Any]] = {}
    for group in discover_navigation_groups(page, screen_size=resolved_size):
        for member in group.members:
            navigation_by_ordinal[member.node_ordinal] = {
                "member_key": member.member_key,
            }

    action_roles: set[str] = set()
    identity_roles: set[str] = set()
    capability_roles: set[str] = set()
    layout_tokens: set[str] = set()
    product_structure_tokens: set[str] = set()
    control_by_anchor: Dict[str, set[str]] = {}
    risk_tokens: set[str] = set()
    family_action_nodes = {
        node.ordinal: node
        for node in visible_nodes
        if (node.clickable or node.editable) and not node.scrollable
    }
    family_action_nodes.update(
        {node.ordinal: node for node in _collapse_overlapping_scroll_nodes(visible_nodes)}
    )
    for node in sorted(family_action_nodes.values(), key=lambda item: item.path):
        if not (node.clickable or node.editable or node.scrollable):
            continue
        semantic_description = node.stable_desc
        semantic_text = node.stable_text
        if not (semantic_description or semantic_text):
            descendant_desc, descendant_text = _descendant_semantics(page, node)
            semantic_description = descendant_desc
            semantic_text = descendant_text
        semantic = semantic_description or semantic_text
        action_type = "input" if node.editable else "scroll" if node.scrollable else "click"
        directions = _scroll_directions(node) if action_type == "scroll" else (None,)
        for direction in directions:
            role, role_key, anchor_key = _action_role_for_node(
                page,
                node,
                action_type=action_type,
                semantic=semantic,
                semantic_description=semantic_description,
                semantic_text=semantic_text,
                screen_size=resolved_size,
                navigation=navigation_by_ordinal.get(node.ordinal),
                direction=direction,
            )
            identity_role = _family_identity_role(page.role, role)
            if not role.startswith(_INSTANCE_ACTION_ROLE_PREFIX):
                capability_roles.add(role)
            if identity_role is None:
                continue
            if catalog_hub and not (
                role == "CATEGORY_TAB:side" or role.startswith("NAV:")
            ):
                # The right pane is sometimes fully semantic and sometimes a
                # single canvas-like node. Those are content capabilities of
                # one two-pane catalog family, not different page families.
                continue
            identity_roles.add(identity_role)
            if page.role == "PRODUCT_DETAIL":
                if identity_role == "SCROLL_CONTAINER":
                    structure = {
                        "class": _class_family(node.class_name),
                        "parent_class": _class_family(
                            visible_by_ordinal[node.parent_ordinal].class_name
                        )
                        if node.parent_ordinal in visible_by_ordinal
                        else "root",
                        "scrollable": node.scrollable,
                    }
                else:
                    structure = _structure_payload(
                        node,
                        by_ordinal=visible_by_ordinal,
                        child_families=child_families,
                    )
                product_structure_tokens.add(
                    _hash_payload(
                        {
                            "product_shell_role": identity_role,
                            "structure": structure,
                        }
                    )
                )
            identity_role_key = _hash_payload({"family_action_role": identity_role})
            action_roles.add(identity_role_key)
            layout_region = _normalized_region(node, resolved_size)
            if page.role == "PRODUCT_DETAIL":
                layout_region = f"product:{identity_role.casefold()}"
            elif role.startswith("CATEGORY_TAB:"):
                layout_region = "category_tab"
            elif role.startswith("ITEM_OPEN:") or (
                page.role == "LIST"
                and role.startswith("COMMAND:")
                and _REPEATED_CARD_TRANSACTION_RE.search(semantic)
            ):
                layout_region = "collection"
            layout_tokens.add(
                _hash_payload(
                    {
                        "role": identity_role,
                        "region": layout_region,
                    }
                )
            )
            if page.role != "PRODUCT_DETAIL":
                control_by_anchor.setdefault(anchor_key, set()).add(
                    f"{int(node.enabled)}:{int(node.checked)}:{int(node.selected)}"
                )
        risk = _default_action_risk(semantic, page.role)
        if risk:
            risk_tokens.add(risk)

    if page.role == "PRODUCT_DETAIL":
        structure_tokens = sorted(product_structure_tokens)
    elif catalog_hub:
        structure_tokens = [
            _hash_payload(
                {
                    "catalog_hub_shell": {
                        "category_rail": True,
                        "content_pane": True,
                    }
                }
            )
        ]

    identity_payload = {
        "version": 2,
        "package": normalize_semantic_text(page.package_name).casefold(),
        "activity_family": page.activity_family,
        "page_role": page.role,
        "page_subtype": page.page_subtype,
        "is_modal": page.role == "DIALOG",
        "is_opaque": page.is_opaque or page.role == "OPAQUE",
        "structure_tokens": structure_tokens,
        "action_role_tokens": sorted(action_roles),
        "layout_tokens": sorted(layout_tokens),
        "control_by_anchor": {
            key: sorted(value) for key, value in sorted(control_by_anchor.items())
        },
    }
    if page.role == "PRODUCT_DETAIL":
        identity_payload["transaction_capability_roles"] = sorted(
            capability_roles & _PRODUCT_DETAIL_TRANSACTION_CAPABILITY_ROLES
        )
    payload = {
        **identity_payload,
        "capability_roles": sorted(capability_roles),
        "risk_tokens": sorted(risk_tokens),
    }
    payload["family_key"] = _hash_payload(identity_payload)
    return payload


def exploration_family_signature(
    page: PageModel,
    *,
    screen_size: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """Return a cardinality-insensitive signature for exploration reuse."""
    return _family_signature(page, screen_size=screen_size)


def _set_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set and not right_set:
        return 1.0
    return len(left_set & right_set) / max(1, len(left_set | right_set))


def compare_exploration_families(
    left: PageModel,
    right: PageModel,
    *,
    left_screen_size: Optional[Tuple[int, int]] = None,
    right_screen_size: Optional[Tuple[int, int]] = None,
) -> ExplorationFamilySimilarity:
    left_signature = _family_signature(left, screen_size=left_screen_size)
    right_signature = _family_signature(right, screen_size=right_screen_size)
    hard_gates = {
        "package_match": left_signature["package"] == right_signature["package"],
        "activity_family_match": (
            left_signature["activity_family"] == right_signature["activity_family"]
        ),
        "role_match": left_signature["page_role"] == right_signature["page_role"],
        "page_subtype_match": (
            left_signature["page_subtype"] == right_signature["page_subtype"]
        ),
        "modal_match": left_signature["is_modal"] == right_signature["is_modal"],
        "opaque_match": left_signature["is_opaque"] == right_signature["is_opaque"],
    }
    risk_match = left_signature["risk_tokens"] == right_signature["risk_tokens"]
    shared_control_anchors = set(left_signature["control_by_anchor"]) & set(
        right_signature["control_by_anchor"]
    )
    control_conflicts = sorted(
        anchor
        for anchor in shared_control_anchors
        if left_signature["control_by_anchor"][anchor]
        != right_signature["control_by_anchor"][anchor]
    )
    hard_gates["shared_control_state_match"] = not control_conflicts

    structure_similarity = _set_similarity(
        left_signature["structure_tokens"], right_signature["structure_tokens"]
    )
    action_similarity = _set_similarity(
        left_signature["action_role_tokens"], right_signature["action_role_tokens"]
    )
    layout_similarity = _set_similarity(
        left_signature["layout_tokens"], right_signature["layout_tokens"]
    )
    raw_score = round(
        0.50 * structure_similarity
        + 0.30 * action_similarity
        + 0.20 * layout_similarity,
        6,
    )
    gates_passed = all(hard_gates.values())
    equivalent = bool(
        gates_passed
        and structure_similarity >= _FAMILY_STRUCTURE_THRESHOLD
        and action_similarity >= _FAMILY_ACTION_THRESHOLD
        and layout_similarity >= _FAMILY_LAYOUT_THRESHOLD
        and raw_score >= _FAMILY_SCORE_THRESHOLD
    )
    evidence = {
        **hard_gates,
        "risk_match": risk_match,
        "risk_is_coverage_variant": True,
        "hard_gates_passed": gates_passed,
        "structure_similarity": round(structure_similarity, 4),
        "action_similarity": round(action_similarity, 4),
        "layout_similarity": round(layout_similarity, 4),
        "control_conflicts": control_conflicts,
        "structure_threshold": _FAMILY_STRUCTURE_THRESHOLD,
        "action_threshold": _FAMILY_ACTION_THRESHOLD,
        "layout_threshold": _FAMILY_LAYOUT_THRESHOLD,
        "score_threshold": _FAMILY_SCORE_THRESHOLD,
    }
    return ExplorationFamilySimilarity(
        score=raw_score if gates_passed else 0.0,
        equivalent=equivalent,
        family_key=str(left_signature["family_key"]),
        evidence=evidence,
    )


def derive_instance_anchor(
    page: PageModel,
    *,
    incoming_action: Optional[InspectionAction] = None,
    source_instance_anchor: Optional[str] = None,
) -> str:
    """Build a conservative business-instance identity without persisting text."""
    if (
        page.role == "PROFILE"
        and page.page_subtype == "PROFILE"
        or page.page_subtype
        in {
            "COMMUNITY_FEED",
            "SETTINGS",
            "ADDRESS_LIST",
            "ADDRESS_FORM",
            "INVOICE_FORM",
        }
    ):
        # Account counters and benefit/order badges are volatile, while a run
        # has exactly one authenticated personal-center and community-feed
        # instance per branch.
        return _hash_payload(
            {
                "version": 2,
                "role": page.role,
                "page_subtype": page.page_subtype,
                "activity_family": page.activity_family,
            }
        )
    incoming_role = str(getattr(incoming_action, "action_role", None) or "")
    if (
        source_instance_anchor
        and incoming_role.startswith(("SORT:", "FILTER_OPEN", "SCROLL:", "CATEGORY_TAB:"))
    ):
        return source_instance_anchor

    if incoming_action is not None:
        metadata = incoming_action.target_meta or {}
        incoming_label = _stable_primary_action_label(
            text=metadata.get("text"),
            content_desc=metadata.get("content_desc"),
        )
        if incoming_label and incoming_role.startswith(("NAV:", "COMMAND:")):
            return _hash_payload(
                {
                    "version": 1,
                    "role": page.role,
                    "incoming": incoming_label.casefold(),
                }
            )

    stable_values = []
    for node in _identity_nodes(page.nodes, page.package_name):
        value = normalize_semantic_text(node.semantic)
        if not value or _DECORATIVE_ITEM_LABEL_RE.fullmatch(value):
            continue
        if _semantic_kind(value) or any(
            pattern.search(value) for _, pattern in _PAGE_ROLE_PATTERNS
        ):
            continue
        stable_values.append(_hash_payload({"value": value.casefold()}))
        if len(stable_values) >= 6:
            break
    return _hash_payload(
        {
            "version": 1,
            "role": page.role,
            "activity_family": page.activity_family,
            "anchors": sorted(stable_values),
            "fallback": page.semantic_key if not stable_values else "",
        }
    )


def _page_identity_signatures(
    identity_nodes: Sequence[SemanticNode],
    *,
    role: str,
) -> Tuple[
    Tuple[str, ...],
    Tuple[str, ...],
    Tuple[str, ...],
    Tuple[str, ...],
    Tuple[str, ...],
]:
    visible_nodes = [node for node in identity_nodes if node.visible]
    by_ordinal = {node.ordinal: node for node in visible_nodes}
    child_families: Dict[int, List[str]] = {}
    for node in visible_nodes:
        if node.parent_ordinal in by_ordinal:
            child_families.setdefault(node.parent_ordinal, []).append(
                _class_family(node.class_name)
            )

    template_tokens: List[str] = []
    action_tokens: List[str] = []
    control_tokens: List[str] = []
    landmark_keys: set[str] = set()
    risk_tokens: set[str] = set()
    for node in visible_nodes:
        structure = _structure_payload(
            node,
            by_ordinal=by_ordinal,
            child_families=child_families,
        )
        structure_token = _hash_payload({"structure": structure})
        template_tokens.append(structure_token)

        semantic = node.semantic
        if not semantic and (node.clickable or node.editable or node.scrollable):
            semantic = next(
                (
                    candidate.semantic
                    for candidate in visible_nodes
                    if len(candidate.path) > len(node.path)
                    and candidate.path[: len(node.path)] == node.path
                    and candidate.semantic
                ),
                "",
            )
        semantic_kind = _semantic_kind(semantic)
        if semantic:
            if role not in _FUZZY_PAGE_ROLES:
                landmark_keys.add(
                    _hash_payload({"landmark": normalize_semantic_text(semantic).casefold()})
                )
            elif semantic_kind:
                landmark_keys.add(_hash_payload({"landmark_kind": semantic_kind}))

        interactive = node.clickable or node.editable or node.scrollable
        if not interactive:
            continue
        risk_type = _default_action_risk(semantic, role)
        if risk_type:
            risk_tokens.add(risk_type)
        action_semantic: Dict[str, Any] = {}
        if semantic_kind:
            action_semantic["kind"] = semantic_kind
        elif role not in _FUZZY_PAGE_ROLES and semantic:
            action_semantic["label_key"] = _hash_payload(
                {"action_label": normalize_semantic_text(semantic).casefold()}
            )
        action_anchor = {
            "structure": structure,
            "semantic": action_semantic,
        }
        action_tokens.append(_hash_payload({"action": action_anchor}))
        control_tokens.append(
            _hash_payload(
                {
                    "action": action_anchor,
                    "enabled": node.enabled,
                    "checked": node.checked,
                    "selected": node.selected,
                }
            )
        )

    return (
        tuple(sorted(set(template_tokens))),
        tuple(sorted(set(action_tokens))),
        tuple(sorted(landmark_keys)),
        tuple(sorted(set(control_tokens))),
        tuple(sorted(risk_tokens)),
    )


def _multiset_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    left_counts = Counter(left)
    right_counts = Counter(right)
    if not left_counts and not right_counts:
        return 1.0
    intersection = sum((left_counts & right_counts).values())
    union = sum((left_counts | right_counts).values())
    return intersection / union if union else 1.0


def compare_page_models(left: PageModel, right: PageModel) -> PageSimilarity:
    """Return conservative evidence that two captures are one logical page.

    Exact semantic identity is always accepted after the package, role and
    activity-family gates.  Fuzzy convergence is deliberately limited to
    recognized page roles; an unknown page must remain exact so two unrelated
    screens with a common container layout are never silently merged.
    """
    package_match = normalize_semantic_text(left.package_name).casefold() == (
        normalize_semantic_text(right.package_name).casefold()
    )
    role_match = bool(left.role) and left.role == right.role
    activity_match = left.activity_family == right.activity_family
    risk_signature_match = left.risk_tokens == right.risk_tokens
    control_state_match = left.control_tokens == right.control_tokens
    hard_gates_passed = (
        package_match
        and role_match
        and activity_match
        and risk_signature_match
        and control_state_match
    )
    same_template = bool(left.template_key) and left.template_key == right.template_key
    exact_semantic = (
        bool(left.semantic_key)
        and left.semantic_key == right.semantic_key
        and hard_gates_passed
    )

    structure_similarity = _multiset_similarity(
        left.template_tokens,
        right.template_tokens,
    )
    action_similarity = _multiset_similarity(left.action_tokens, right.action_tokens)
    landmark_similarity = _multiset_similarity(left.landmark_keys, right.landmark_keys)
    control_similarity = _multiset_similarity(
        left.control_tokens,
        right.control_tokens,
    )
    raw_score = round(
        0.40 * structure_similarity
        + 0.30 * action_similarity
        + 0.20 * landmark_similarity
        + 0.10 * control_similarity,
        6,
    )
    high_confidence_threshold = 0.92
    gray_zone_threshold = 0.82
    high_confidence = (
        raw_score >= high_confidence_threshold
        and structure_similarity >= 0.90
        and action_similarity >= 0.85
    )
    fuzzy_equivalent = (
        hard_gates_passed
        and left.role in _FUZZY_PAGE_ROLES
        and high_confidence
    )
    equivalent = exact_semantic or fuzzy_equivalent
    score = raw_score if hard_gates_passed else 0.0
    confidence_band = (
        "HIGH"
        if hard_gates_passed and high_confidence
        else "GRAY"
        if hard_gates_passed and raw_score >= gray_zone_threshold
        else "LOW"
    )
    evidence = {
        "version": 2,
        "package_match": package_match,
        "role_match": role_match,
        "activity_family_match": activity_match,
        "risk_signature_match": risk_signature_match,
        "control_state_match": control_state_match,
        "hard_gates_passed": hard_gates_passed,
        "exact_semantic_match": exact_semantic,
        "exact_only_role": (
            left.role not in _FUZZY_PAGE_ROLES
            or right.role not in _FUZZY_PAGE_ROLES
        ),
        "confidence_band": confidence_band,
        "structure_similarity": round(structure_similarity, 4),
        "action_similarity": round(action_similarity, 4),
        "anchor_similarity": round(landmark_similarity, 4),
        "landmark_similarity": round(landmark_similarity, 4),
        "control_similarity": round(control_similarity, 4),
        "high_confidence_threshold": high_confidence_threshold,
        "gray_zone_threshold": gray_zone_threshold,
        "left_template_token_count": len(left.template_tokens),
        "right_template_token_count": len(right.template_tokens),
        "left_action_token_count": len(left.action_tokens),
        "right_action_token_count": len(right.action_tokens),
        "left_landmark_count": len(left.landmark_keys),
        "right_landmark_count": len(right.landmark_keys),
        "left_control_token_count": len(left.control_tokens),
        "right_control_token_count": len(right.control_tokens),
    }
    return PageSimilarity(
        score=score,
        equivalent=equivalent,
        same_template=same_template,
        candidate_semantic_key=right.semantic_key,
        candidate_template_key=right.template_key,
        evidence=evidence,
    )


def build_page_model(
    xml: str,
    *,
    package_name: str,
    activity: str,
    screenshot_phash: str = "",
    dynamic_patterns: Optional[Sequence[str]] = None,
    max_text_length: int = 80,
) -> PageModel:
    """Build inspection-only cluster/state identities from one hierarchy."""
    try:
        root = ET.fromstring(str(xml or ""))
    except ET.ParseError as exc:
        raise ValueError(f"invalid Android hierarchy XML: {exc}") from exc

    nodes = _walk_nodes(
        root,
        dynamic_patterns=dynamic_patterns,
        max_text_length=max_text_length,
    )
    identity_nodes = _coverage_identity_nodes(nodes, package_name)
    role = _infer_page_role(identity_nodes, activity)
    page_subtype = _infer_page_subtype(
        identity_nodes,
        role=role,
        package_name=package_name,
    )
    if page_subtype in {
        "STORE_LIST",
        "PRODUCT_LIST",
        "FAVORITES",
        "BROWSING_HISTORY",
    }:
        role = "LIST"
    elif page_subtype == "ORDER":
        role = "ORDER"
    activity_family = _activity_family(activity)
    (
        template_tokens,
        action_tokens,
        landmark_keys,
        control_tokens,
        risk_tokens,
    ) = _page_identity_signatures(identity_nodes, role=role)
    normalized_package = normalize_semantic_text(package_name).casefold()
    template_key = _hash_payload(
        {
            "version": 2,
            "package": normalized_package,
            "activity_family": activity_family,
            "role": role,
            "page_subtype": page_subtype,
            "template_tokens": template_tokens,
        }
    )
    semantic_key = _hash_payload(
        {
            "version": 2,
            "package": normalized_package,
            "activity_family": activity_family,
            "role": role,
            "page_subtype": page_subtype,
            "template_key": template_key,
            "action_tokens": action_tokens,
            "landmark_keys": landmark_keys,
            "control_tokens": control_tokens,
            "risk_tokens": risk_tokens,
        }
    )
    cluster_payload = {
        "package": str(package_name or ""),
        "activity": str(activity or ""),
        "tree": _canonical_tree(identity_nodes, include_state=False),
    }
    cluster_key = _hash_payload(cluster_payload)
    replay_payload = {
        "cluster": cluster_key,
        "tree": _canonical_tree(identity_nodes, include_state=True),
    }
    replay_key = _hash_payload(replay_payload)
    state_payload = {
        "replay": replay_key,
        "phash": str(screenshot_phash or ""),
    }
    actionable = [
        node
        for node in nodes
        if node.visible
        and node.enabled
        and (node.clickable or node.editable or node.scrollable)
    ]
    has_dynamic = any(
        (node.content_desc and not node.stable_desc)
        or (node.text and not node.stable_text)
        for node in identity_nodes
    )
    opaque_container = any(
        any(
            hint in node.class_name.lower()
            for hint in ("webview", "surfaceview", "canvas", "mapview")
        )
        for node in nodes
    )
    semantic_actionable = any(node.semantic for node in actionable)
    return PageModel(
        xml=xml,
        package_name=package_name,
        activity=activity,
        nodes=nodes,
        cluster_key=cluster_key,
        replay_key=replay_key,
        state_key=_hash_payload(state_payload),
        is_opaque=(
            (not actionable and len(nodes) <= 3)
            or (opaque_container and not semantic_actionable)
        ),
        has_dynamic_text=has_dynamic,
        role=role,
        page_subtype=page_subtype,
        template_key=template_key,
        semantic_key=semantic_key,
        activity_family=activity_family,
        screenshot_phash=str(screenshot_phash or ""),
        template_tokens=template_tokens,
        action_tokens=action_tokens,
        landmark_keys=landmark_keys,
        control_tokens=control_tokens,
        risk_tokens=risk_tokens,
        _by_ordinal={node.ordinal: node for node in nodes},
    )


def _nearest_stable_ancestor(page: PageModel, node: SemanticNode) -> Optional[SemanticNode]:
    current = page.node(node.parent_ordinal)
    while current is not None:
        if current.stable_desc or current.stable_text:
            return current
        current = page.node(current.parent_ordinal)
    return None


def _nearest_clickable_ancestor(page: PageModel, node: SemanticNode) -> Optional[SemanticNode]:
    current: Optional[SemanticNode] = node
    while current is not None:
        if current.clickable and current.enabled and current.visible:
            return current
        current = page.node(current.parent_ordinal)
    return None


def _descendant_semantics(page: PageModel, node: SemanticNode) -> Tuple[str, str]:
    prefix = node.path
    fallback: Tuple[str, str] = ("", "")
    for candidate in page.nodes:
        if (
            len(candidate.path) > len(prefix)
            and candidate.path[: len(prefix)] == prefix
            and (candidate.stable_desc or candidate.stable_text)
        ):
            value = normalize_semantic_text(
                candidate.stable_desc or candidate.stable_text
            )
            if not fallback[0] and not fallback[1]:
                fallback = (candidate.stable_desc, candidate.stable_text)
            if value and not _DECORATIVE_ITEM_LABEL_RE.fullmatch(value):
                return candidate.stable_desc, candidate.stable_text
    return fallback


def _is_collection_card(
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    if node.bounds is None:
        return False
    width = (node.bounds[2] - node.bounds[0]) / max(1, screen_size[0])
    height = (node.bounds[3] - node.bounds[1]) / max(1, screen_size[1])
    return width >= 0.70 and height >= 0.05


def _is_catalog_sidebar_member(
    page: PageModel,
    node: SemanticNode,
) -> bool:
    return any(
        candidate.ordinal == node.ordinal
        for candidate in _catalog_sidebar_members(page.nodes)
    )


def _catalog_sidebar_member_is_selected(
    page: PageModel,
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    """Detect the mall's narrow blue selected indicator structurally."""
    if node.bounds is None:
        return False
    screen_width, _ = screen_size
    node_height = max(1, node.bounds[3] - node.bounds[1])
    for descendant in _subtree_nodes(page, node):
        if descendant.ordinal == node.ordinal or descendant.bounds is None:
            continue
        left, top, right, bottom = descendant.bounds
        width = right - left
        height = bottom - top
        if (
            not descendant.semantic
            and left <= node.bounds[0] + max(2, int(screen_width * 0.005))
            and width <= max(16, int(screen_width * 0.02))
            and height >= node_height * 0.70
            and top >= node.bounds[1]
            and bottom <= node.bounds[3]
        ):
            return True
    return False


def _is_catalog_grid_entry(
    page: PageModel,
    node: SemanticNode,
    screen_size: Tuple[int, int],
) -> bool:
    """Recognize one image tile in the right pane of the category hub."""
    if page.page_subtype != "CATALOG_CATEGORY" or node.bounds is None:
        return False
    screen_width, screen_height = screen_size
    left, _, right, _ = node.bounds
    width = (right - left) / max(1, screen_width)
    height = (node.bounds[3] - node.bounds[1]) / max(1, screen_height)
    if not (
        left / max(1, screen_width) >= 0.20
        and 0.12 <= width <= 0.36
        and 0.05 <= height <= 0.20
    ):
        return False
    return any(
        descendant.ordinal != node.ordinal
        and descendant.visible
        and "image" in descendant.class_name.casefold()
        for descendant in _subtree_nodes(page, node)
    )


def _looks_like_product_title(value: Any) -> bool:
    text = normalize_semantic_text(value)
    if not (4 <= len(text) <= 120):
        return False
    if (
        _DECORATIVE_ITEM_LABEL_RE.fullmatch(text)
        or _PRODUCT_SPEC_LABEL_RE.fullmatch(text)
        or _NON_TITLE_CARD_LABEL_RE.fullmatch(text)
        or _MONEY_RE.search(text)
        or _DATE_TIME_RE.search(text)
        or _REPEATED_CARD_TRANSACTION_RE.fullmatch(text)
        or re.match(r"^[①②③④⑤⑥⑦⑧⑨【]", text)
    ):
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text))


def _product_card_title(
    page: PageModel,
    node: SemanticNode,
    *,
    screen_size: Tuple[int, int],
) -> Tuple[str, str, Optional[SemanticNode]]:
    """Return the rendered title of a full-width list card.

    Product accessibility descriptions commonly append price, benefits and
    specifications. A title TextView is therefore preferred; the first segment
    of the card description is only a fallback.
    """
    if page.role != "LIST" or not _is_collection_card(node, screen_size):
        return "", "", None
    descendants = [
        candidate
        for candidate in _subtree_nodes(page, node)
        if candidate.ordinal != node.ordinal and candidate.visible
    ]
    text_candidates = [
        candidate
        for candidate in descendants
        if _class_family(candidate.class_name) == "text"
        and _looks_like_product_title(candidate.text)
    ]
    if text_candidates:
        title_node = min(
            text_candidates,
            key=lambda candidate: (
                candidate.bounds[1] if candidate.bounds else screen_size[1],
                candidate.path,
            ),
        )
        return "", normalize_semantic_text(title_node.text), title_node
    for raw_description in (node.content_desc, node.text):
        first_segment = next(
            (
                normalize_semantic_text(segment)
                for segment in _INSTANCE_ENTRY_LABEL_SPLIT_RE.split(raw_description or "")
                if normalize_semantic_text(segment)
            ),
            "",
        )
        if _looks_like_product_title(first_segment):
            return first_segment if raw_description == node.content_desc else "", (
                first_segment if raw_description == node.text else ""
            ), node
    return "", "", None


def _clickable_ancestor_title_locator(
    page: PageModel,
    click_node: SemanticNode,
    title_node: Optional[SemanticNode],
    *,
    title: str,
) -> Optional[Dict[str, Any]]:
    if title_node is None or not title or title_node.ordinal == click_node.ordinal:
        return None
    matching_titles = [
        candidate
        for candidate in page.nodes
        if candidate.visible and normalize_semantic_text(candidate.text) == title
    ]
    matching_clickables: List[SemanticNode] = []
    for candidate_title in matching_titles:
        ancestor = _nearest_clickable_ancestor(page, candidate_title)
        if ancestor is not None:
            matching_clickables.append(ancestor)
    if len(matching_clickables) != 1 or matching_clickables[0].ordinal != click_node.ordinal:
        return None
    selector = (
        f"(//node[contains(@text, {_xpath_literal(title)})]/"
        f"ancestor::node[@class={_xpath_literal(click_node.class_name)} and "
        f"@clickable='true'][1])[1]"
    )
    return {
        "selector": selector,
        "by": "xpath",
        "expected_class": click_node.class_name,
        "target_descendant_text": title,
        "requires_clickable_ancestor": True,
        "nearest_clickable_ancestor": True,
        "ordinal": 1,
    }


def _subtree_nodes(page: PageModel, node: SemanticNode) -> Iterable[SemanticNode]:
    prefix = node.path
    for candidate in page.nodes:
        if candidate.ordinal == node.ordinal or (
            len(candidate.path) > len(prefix)
            and candidate.path[: len(prefix)] == prefix
        ):
            yield candidate


def _navigation_label(page: PageModel, node: SemanticNode) -> str:
    """Return a stable, user-visible label for one navigation item.

    React Native commonly exposes a volatile parent description such as
    ``购物车, 20`` while retaining a stable ``购物车`` TextView below it.  Text
    descendants therefore take precedence over descendant descriptions when
    the clickable parent itself has no stable semantic value.
    """
    descendants = list(_subtree_nodes(page, node))[1:]
    # A stable text child is usually the rendered Tab caption.  Prefer it over
    # a parent accessibility description, which often appends volatile badge
    # values such as ``购物车(20)`` or ``购物车，20``.
    stable_text = next((item.stable_text for item in descendants if item.stable_text), "")
    if stable_text:
        return stable_text
    if node.stable_desc:
        return node.stable_desc
    if node.stable_text:
        return node.stable_text
    return next((item.stable_desc for item in descendants if item.stable_desc), "")


def _normalized_bounds(
    bounds: Tuple[int, int, int, int],
    screen_size: Tuple[int, int],
) -> Tuple[float, float, float, float]:
    width, height = screen_size
    x1, y1, x2, y2 = bounds
    return (
        round(x1 / width, 6),
        round(y1 / height, 6),
        round(x2 / width, 6),
        round(y2 / height, 6),
    )


def _navigation_label_key(value: Any) -> str:
    return normalize_semantic_text(value).casefold()


def _navigation_union_coverage(
    nodes: Sequence[SemanticNode],
    screen_width: int,
) -> float:
    intervals = sorted(
        (
            max(0, node.bounds[0]),
            min(screen_width, node.bounds[2]),
        )
        for node in nodes
        if node.bounds is not None
    )
    if not intervals or screen_width <= 0:
        return 0.0
    covered = 0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        covered += max(0, current_end - current_start)
        current_start, current_end = start, end
    covered += max(0, current_end - current_start)
    return min(1.0, covered / screen_width)


def _has_dialog_ancestor(page: PageModel, parent_ordinal: Optional[int]) -> bool:
    current = page.node(parent_ordinal)
    while current is not None:
        if _DIALOG_CLASS_RE.search(f"{current.class_name} {current.resource_id}"):
            return True
        current = page.node(current.parent_ordinal)
    return False


def _has_opaque_ancestor(page: PageModel, node_ordinal: Optional[int]) -> bool:
    current = page.node(node_ordinal)
    while current is not None:
        if _OPAQUE_CLASS_RE.search(f"{current.class_name} {current.resource_id}"):
            return True
        current = page.node(current.parent_ordinal)
    return False


def _page_has_modal(page: PageModel) -> bool:
    return any(
        node.visible
        and _MODAL_CLASS_RE.search(f"{node.class_name} {node.resource_id}")
        for node in page.nodes
    )


def _has_near_scrollable_ancestor(
    page: PageModel,
    parent_ordinal: Optional[int],
    *,
    max_hops: int = 2,
) -> bool:
    current = page.node(parent_ordinal)
    for _ in range(max_hops + 1):
        if current is None:
            return False
        if current.scrollable:
            return True
        current = page.node(current.parent_ordinal)
    return False


def _navigation_group_parent(
    page: PageModel,
    node: SemanticNode,
    candidates: Sequence[SemanticNode],
    *,
    max_wrapper_hops: int = 2,
) -> Optional[int]:
    """Collapse up to two single-item wrappers around a Tab click target."""
    original_parent = node.parent_ordinal
    current = page.node(original_parent)
    wrapper_hops = 0
    while current is not None:
        prefix = current.path
        descendant_count = sum(
            1
            for candidate in candidates
            if len(candidate.path) > len(prefix)
            and candidate.path[: len(prefix)] == prefix
        )
        if descendant_count >= _NAVIGATION_MIN_MEMBERS:
            return current.ordinal
        if descendant_count != 1 or wrapper_hops >= max_wrapper_hops:
            break
        wrapper_hops += 1
        current = page.node(current.parent_ordinal)
    return original_parent


def _navigation_content_key(
    page: PageModel,
    groups: Sequence[NavigationGroup],
) -> str:
    """Hash page content after removing all candidate navigation subtrees."""
    excluded_prefixes: List[Tuple[int, ...]] = []
    for group in groups:
        # React Native frequently mounts the page body and every bottom-tab
        # click target under one large ViewGroup.  Excluding the common parent
        # would therefore erase the very content change used to confirm a Tab
        # switch.  Remove only each member subtree; this still ignores
        # selected/checked flags, icons and dynamic badges owned by the Tab.
        for member in group.members:
            node = page.node(member.node_ordinal)
            if node is not None:
                excluded_prefixes.append(node.path)
    identity_nodes = [
        node
        for node in _identity_nodes(page.nodes, page.package_name)
        if not any(
            len(node.path) >= len(prefix)
            and node.path[: len(prefix)] == prefix
            for prefix in excluded_prefixes
        )
    ]
    return _hash_payload(
        {
            "package": str(page.package_name or "").casefold(),
            "activity": str(page.activity or "").casefold(),
            "content_tree": _canonical_tree(identity_nodes, include_state=True),
        }
    )


def _member_active_flags(
    page: PageModel,
    node: SemanticNode,
) -> Tuple[bool, bool]:
    subtree = list(_subtree_nodes(page, node))
    return (
        any(candidate.selected for candidate in subtree),
        any(candidate.checked for candidate in subtree),
    )


def _member_has_indicator(page: PageModel, node: SemanticNode) -> bool:
    if node.bounds is None:
        return False
    x1, y1, x2, y2 = node.bounds
    item_width = x2 - x1
    item_height = y2 - y1
    descendants = list(_subtree_nodes(page, node))[1:]
    siblings = [
        candidate
        for candidate in page.nodes
        if candidate.parent_ordinal == node.parent_ordinal
        and candidate.ordinal != node.ordinal
    ]
    sibling_ordinals = {candidate.ordinal for candidate in siblings}
    for candidate in [*descendants, *siblings]:
        if not candidate.visible or candidate.bounds is None or candidate.semantic:
            continue
        cx1, cy1, cx2, cy2 = candidate.bounds
        width = cx2 - cx1
        height = cy2 - cy1
        overlaps_member = cx2 > x1 and cx1 < x2
        if candidate.ordinal in sibling_ordinals and not overlaps_member:
            continue
        hinted = bool(
            _INDICATOR_HINT_RE.search(
                f"{candidate.class_name} {candidate.resource_id}"
            )
        )
        underline_shape = bool(
            width >= item_width * 0.20
            and height <= max(8, item_height * 0.12)
            and cy1 >= y1 + item_height * 0.60
            and cy2 <= y2 + max(8, item_height * 0.08)
        )
        if hinted or underline_shape:
            return True
    return False


def _navigation_region(
    nodes: Sequence[SemanticNode],
    screen_height: int,
) -> str:
    if not nodes or screen_height <= 0:
        return ""
    top = min(node.bounds[1] for node in nodes if node.bounds is not None)
    bottom = max(node.bounds[3] for node in nodes if node.bounds is not None)
    center = (top + bottom) / 2 / screen_height
    if center <= 0.20:
        return "top"
    if center >= 0.72 and bottom / screen_height >= 0.80:
        return "bottom"
    return ""


def discover_navigation_groups(
    page: PageModel,
    *,
    screen_size: Tuple[int, int],
) -> List[NavigationGroup]:
    """Discover conservative top/bottom Tab candidates from one element tree.

    A candidate consists of two to seven semantic click targets under one
    navigation container, optionally separated by one or two single-item
    wrappers.  Bottom navigation additionally needs at least three items
    covering 70% of the screen.  A top row without an active-state signal must
    be a strict two-item Tab candidate and still needs destination confirmation.
    """
    screen_width, screen_height = screen_size
    if screen_width <= 0 or screen_height <= 0:
        return []

    target_package = str(page.package_name or "").strip().casefold()
    eligible: List[SemanticNode] = []
    for node in page.nodes:
        node_package = str(node.node_package or "").strip().casefold()
        if (
            not node.visible
            or not node.enabled
            or not node.clickable
            or node.editable
            or node.scrollable
            or node.bounds is None
            or node.parent_ordinal is None
            or (target_package and node_package and node_package != target_package)
        ):
            continue
        if _navigation_label(page, node) and not _has_opaque_ancestor(
            page, node.ordinal
        ):
            eligible.append(node)

    by_parent: Dict[int, List[SemanticNode]] = {}
    for node in eligible:
        parent_ordinal = _navigation_group_parent(page, node, eligible)
        if parent_ordinal is not None:
            by_parent.setdefault(parent_ordinal, []).append(node)

    groups: List[NavigationGroup] = []
    for parent_ordinal, candidates in by_parent.items():
        if not (_NAVIGATION_MIN_MEMBERS <= len(candidates) <= _NAVIGATION_MAX_MEMBERS):
            continue
        if (
            _has_dialog_ancestor(page, parent_ordinal)
            or _has_opaque_ancestor(page, parent_ordinal)
            or _has_near_scrollable_ancestor(
                page, parent_ordinal, max_hops=3
            )
        ):
            continue

        ordered = sorted(
            candidates,
            key=lambda item: (
                (item.bounds[0] + item.bounds[2]) / 2 if item.bounds else 0,
                item.path,
            ),
        )
        labels = [_navigation_label(page, node) for node in ordered]
        label_keys = [_navigation_label_key(label) for label in labels]
        if (
            any(not key or len(label) > 32 for key, label in zip(label_keys, labels))
            or len(set(label_keys)) != len(label_keys)
            or any(_NAVIGATION_ACTION_RE.search(label) for label in labels)
        ):
            continue

        widths = [node.bounds[2] - node.bounds[0] for node in ordered if node.bounds]
        heights = [node.bounds[3] - node.bounds[1] for node in ordered if node.bounds]
        if (
            not widths
            or not heights
            or min(widths) / max(widths) < 0.45
            or min(heights) / max(heights) < 0.60
        ):
            continue
        centers_y = [
            (node.bounds[1] + node.bounds[3]) / 2
            for node in ordered
            if node.bounds
        ]
        if max(centers_y) - min(centers_y) > max(12, screen_height * 0.03):
            continue
        overlap = any(
            right.bounds[0] < left.bounds[2] - min(widths) * 0.10
            for left, right in zip(ordered, ordered[1:])
            if left.bounds and right.bounds
        )
        if overlap:
            continue

        region = _navigation_region(ordered, screen_height)
        if not region:
            continue
        coverage = _navigation_union_coverage(ordered, screen_width)
        if region == "bottom" and (
            len(ordered) < _BOTTOM_NAVIGATION_MIN_MEMBERS
            or coverage < _BOTTOM_NAVIGATION_MIN_COVERAGE
        ):
            continue

        members: List[NavigationMember] = []
        for index, (node, label) in enumerate(zip(ordered, labels)):
            selected, checked = _member_active_flags(page, node)
            members.append(
                NavigationMember(
                    label=label,
                    index=index,
                    node_ordinal=node.ordinal,
                    class_name=node.class_name,
                    normalized_bounds=_normalized_bounds(node.bounds, screen_size),
                    selected=selected,
                    checked=checked,
                    has_indicator=_member_has_indicator(page, node),
                )
            )

        if region == "top":
            gaps = [
                max(0, right.bounds[0] - left.bounds[2])
                for left, right in zip(ordered, ordered[1:])
                if left.bounds and right.bounds
            ]
            group_height = max(
                node.bounds[3] for node in ordered if node.bounds
            ) - min(node.bounds[1] for node in ordered if node.bounds)
            if (
                coverage < 0.20
                or group_height / screen_height > 0.08
                or (gaps and max(gaps) > max(widths) * 1.75)
                or (
                    not any(member.active for member in members)
                    and (
                        len(members) != 2
                        or coverage < 0.45
                        or min(widths) / max(widths) < 0.80
                        or (gaps and max(gaps) > max(widths) * 0.25)
                    )
                )
            ):
                continue

        group_bounds = (
            min(node.bounds[0] for node in ordered if node.bounds),
            min(node.bounds[1] for node in ordered if node.bounds),
            max(node.bounds[2] for node in ordered if node.bounds),
            max(node.bounds[3] for node in ordered if node.bounds),
        )
        parent = page.node(parent_ordinal)
        parent_class = parent.class_name if parent is not None else ""
        normalized_group_bounds = _normalized_bounds(group_bounds, screen_size)
        key_payload = {
            "region": region,
            "labels": label_keys,
            "parent_class": parent_class,
            "member_classes": [member.class_name for member in members],
            "geometry_bucket": [
                round(value, 1) for value in normalized_group_bounds
            ],
        }
        unique_active = sum(1 for member in members if member.active) == 1
        base_confidence = 0.88 if region == "bottom" else 0.80
        candidate_confidence = min(
            0.96,
            base_confidence
            + (0.03 if min(widths) / max(widths) >= 0.80 else 0.0)
            + (0.02 if unique_active else 0.0)
            + (0.02 if coverage >= 0.90 else 0.0),
        )
        groups.append(
            NavigationGroup(
                group_key=_hash_payload(key_payload),
                region=region,
                parent_ordinal=parent_ordinal,
                parent_class=parent_class,
                coverage=coverage,
                normalized_bounds=normalized_group_bounds,
                members=tuple(members),
                candidate_confidence=candidate_confidence,
            )
        )

    return sorted(
        groups,
        key=lambda group: (-group.candidate_confidence, group.region, group.group_key),
    )


def navigation_metadata_for_action(
    page: PageModel,
    group: NavigationGroup,
    member: NavigationMember,
    *,
    all_groups: Optional[Sequence[NavigationGroup]] = None,
) -> Dict[str, Any]:
    """Build the safe, JSON-serializable metadata persisted with a Tab click."""
    member_evidence = member.to_dict()
    return {
        "group_key": group.group_key,
        "group_region": group.region,
        "group_bounds": list(group.normalized_bounds),
        "member_key": member.member_key,
        "member_index": member.index,
        "member_count": len(group.members),
        "member": member_evidence,
        "members": [item.to_dict() for item in group.members],
        "active_member_indices": [
            item.index for item in group.members if item.active
        ],
        "candidate_confidence": round(group.candidate_confidence, 4),
        "source_package_key": _hash_payload(
            {"package": str(page.package_name or "").casefold()}
        ),
        "source_activity_key": _hash_payload(
            {"activity": str(page.activity or "").casefold()}
        ),
        "source_content_key": _navigation_content_key(
            page, all_groups if all_groups is not None else (group,)
        ),
    }


def _node_is_inside_navigation_member(
    page: PageModel,
    node: SemanticNode,
    groups: Sequence[NavigationGroup],
) -> bool:
    for group in groups:
        for member in group.members:
            member_node = page.node(member.node_ordinal)
            if member_node is not None and (
                node.path == member_node.path
                or (
                    len(node.path) > len(member_node.path)
                    and node.path[: len(member_node.path)] == member_node.path
                )
            ):
                return True
    return False


def _node_center_occluded_by_navigation(
    page: PageModel,
    node: SemanticNode,
    groups: Sequence[NavigationGroup],
    *,
    screen_size: Tuple[int, int],
) -> bool:
    """Reject stale scroll children whose tap point sits behind a fixed Tab bar."""
    if node.bounds is None or _node_is_inside_navigation_member(page, node, groups):
        return False
    screen_width, screen_height = screen_size
    touch_target = node
    if not (node.clickable or node.editable or node.scrollable):
        touch_target = _nearest_clickable_ancestor(page, node) or node
    target_bounds = touch_target.bounds or node.bounds
    center_x = (target_bounds[0] + target_bounds[2]) / 2
    center_y = (target_bounds[1] + target_bounds[3]) / 2
    if not (0 <= center_x <= screen_width and 0 <= center_y <= screen_height):
        return True
    inside_scrolling_content = _has_near_scrollable_ancestor(
        page,
        touch_target.parent_ordinal,
        max_hops=len(touch_target.path),
    )
    for group in groups:
        if group.region not in {"top", "bottom"}:
            continue
        left, top, right, bottom = group.normalized_bounds
        same_vertical_band = top * screen_height <= center_y <= bottom * screen_height
        same_horizontal_band = left * screen_width <= center_x <= right * screen_width
        clipped_behind_top_bar = bool(
            group.region == "top"
            and inside_scrolling_content
            and target_bounds[1] <= 0
        )
        if same_vertical_band and (same_horizontal_band or clipped_behind_top_bar):
            return True
    return False


def _navigation_action_is_current(
    page: PageModel,
    navigation: Optional[Dict[str, Any]],
    semantic: str,
) -> bool:
    if not navigation:
        return False
    member = navigation.get("member")
    if isinstance(member, dict) and bool(member.get("active")):
        return True
    # Some React Native bottom bars expose active color only through pixels.
    # HOME is still unambiguous when the page itself has been classified HOME.
    return bool(page.role == "HOME" and _semantic_kind(semantic) == "HOME")


def _metadata_center(member: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    bounds = member.get("normalized_bounds")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(value) for value in bounds)
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) / 2, (y1 + y2) / 2


def confirm_peer_navigation(
    source_navigation: Dict[str, Any],
    target_page: PageModel,
    *,
    screen_size: Tuple[int, int],
    confidence_threshold: float = _PEER_NAVIGATION_CONFIDENCE,
) -> NavigationConfirmation:
    """Confirm a peer transition using source action metadata and target tree."""
    raw_navigation = source_navigation or {}
    if isinstance(raw_navigation.get("navigation"), dict):
        raw_navigation = raw_navigation["navigation"]

    source_members = raw_navigation.get("members")
    if not isinstance(source_members, list):
        source_members = []
    source_by_key = {
        str(member.get("member_key") or ""): member
        for member in source_members
        if isinstance(member, dict) and str(member.get("member_key") or "")
    }
    source_keys = list(source_by_key)
    source_group_key = str(raw_navigation.get("group_key") or "")
    source_region = str(raw_navigation.get("group_region") or "")
    clicked_member_key = str(raw_navigation.get("member_key") or "")
    source_package_key = str(raw_navigation.get("source_package_key") or "")
    target_package_key = _hash_payload(
        {"package": str(target_page.package_name or "").casefold()}
    )
    source_content_key = str(raw_navigation.get("source_content_key") or "")
    same_package = bool(
        source_package_key and source_package_key == target_package_key
    )
    source_active_keys = [
        str(member.get("member_key") or "")
        for member in source_members
        if isinstance(member, dict)
        and bool(member.get("active"))
        and str(member.get("member_key") or "")
    ]
    source_active_count = len(source_active_keys)
    target_modal = _page_has_modal(target_page)

    target_groups = discover_navigation_groups(
        target_page,
        screen_size=screen_size,
    )
    target_content_key = _navigation_content_key(target_page, target_groups)
    page_changed = bool(
        source_content_key and source_content_key != target_content_key
    )

    best_group: Optional[NavigationGroup] = None
    best_confidence = 0.0
    best_evidence: Dict[str, Any] = {
        "same_package": same_package,
        "page_changed": page_changed,
        "label_overlap": 0.0,
        "member_overlap": 0.0,
        "coordinate_deviation": None,
        "coordinate_within_tolerance": False,
        "region_match": False,
        "clicked_member_present": False,
        "group_key_match": False,
        "ordered_label_overlap": 0.0,
        "active_signal_bonus": False,
        "source_active_member_count": source_active_count,
        "target_active_member_count": 0,
        "clicked_unique_active": False,
        "active_state_valid": source_active_count == 0,
        "target_modal": target_modal,
        "threshold": confidence_threshold,
    }
    best_gates = False

    for target_group in target_groups:
        target_by_key = {
            member.member_key: member
            for member in target_group.members
        }
        target_keys = list(target_by_key)
        shared_keys = set(source_keys) & set(target_keys)
        denominator = max(len(source_keys), len(target_keys), 1)
        member_overlap = len(shared_keys) / denominator
        coordinate_deviation = float("inf")
        if shared_keys:
            deviations: List[float] = []
            for member_key in shared_keys:
                source_center = _metadata_center(source_by_key[member_key])
                target_bounds = target_by_key[member_key].normalized_bounds
                target_center = (
                    (target_bounds[0] + target_bounds[2]) / 2,
                    (target_bounds[1] + target_bounds[3]) / 2,
                )
                if source_center is None:
                    deviations = []
                    break
                deviations.append(
                    max(
                        abs(source_center[0] - target_center[0]),
                        abs(source_center[1] - target_center[1]),
                    )
                )
            if deviations:
                coordinate_deviation = max(deviations)

        source_order = [
            str(member.get("member_key") or "")
            for member in source_members
            if isinstance(member, dict)
        ]
        ordered_matches = sum(
            1
            for source_key, target_key in zip(source_order, target_keys)
            if source_key == target_key
        )
        ordered_overlap = ordered_matches / denominator
        coordinate_score = max(
            0.0,
            1.0 - coordinate_deviation / _NAVIGATION_COORDINATE_TOLERANCE,
        )
        group_key_match = bool(
            source_group_key and source_group_key == target_group.group_key
        )
        region_match = bool(source_region and source_region == target_group.region)
        clicked_member_present = bool(
            clicked_member_key and clicked_member_key in target_by_key
        )
        clicked_target = target_by_key.get(clicked_member_key)
        target_active_count = target_group.active_member_count
        clicked_unique_active = bool(
            clicked_target is not None
            and clicked_target.active
            and target_active_count == 1
        )
        if source_active_count == 0:
            active_state_valid = bool(
                target_active_count == 0 or clicked_unique_active
            )
        else:
            active_state_valid = bool(
                source_active_count == 1
                and source_active_keys[0] != clicked_member_key
                and clicked_unique_active
            )
        active_signal_bonus = bool(
            clicked_unique_active
        )
        confidence = min(
            1.0,
            0.47
            + 0.22 * member_overlap
            + 0.13 * coordinate_score
            + 0.08 * ordered_overlap
            + (0.05 if group_key_match else 0.0)
            + (0.03 if region_match else 0.0)
            + (0.02 if active_signal_bonus else 0.0),
        )
        coordinate_ok = coordinate_deviation <= _NAVIGATION_COORDINATE_TOLERANCE
        gates = bool(
            same_package
            and page_changed
            and member_overlap >= _NAVIGATION_LABEL_OVERLAP
            and coordinate_ok
            and region_match
            and clicked_member_present
            and active_state_valid
            and not target_modal
        )
        evidence = {
            "same_package": same_package,
            "page_changed": page_changed,
            "label_overlap": round(member_overlap, 4),
            "member_overlap": round(member_overlap, 4),
            "coordinate_deviation": (
                round(coordinate_deviation, 6)
                if coordinate_deviation != float("inf")
                else None
            ),
            "coordinate_within_tolerance": coordinate_ok,
            "region_match": region_match,
            "clicked_member_present": clicked_member_present,
            "group_key_match": group_key_match,
            "ordered_label_overlap": round(ordered_overlap, 4),
            "active_signal_bonus": active_signal_bonus,
            "source_active_member_count": source_active_count,
            "target_active_member_count": target_active_count,
            "clicked_unique_active": clicked_unique_active,
            "active_state_valid": active_state_valid,
            "target_modal": target_modal,
            "threshold": confidence_threshold,
        }
        if (
            best_group is None
            or (gates, confidence, member_overlap)
            > (best_gates, best_confidence, best_evidence["label_overlap"])
        ):
            best_group = target_group
            best_confidence = confidence
            best_evidence = evidence
            best_gates = gates

    matched = bool(best_gates and best_confidence >= confidence_threshold)
    return NavigationConfirmation(
        matched=matched,
        confidence=best_confidence,
        group_key=source_group_key or (best_group.group_key if best_group else ""),
        evidence=best_evidence,
        target_group=best_group,
    )


def _semantic_locator_node(
    page: PageModel,
    node: SemanticNode,
    *,
    semantic_desc: str,
    semantic_text: str,
) -> SemanticNode:
    """Use the semantic child as locator target for a semantic-less click parent."""
    if (
        (semantic_desc and node.content_desc == semantic_desc)
        or (not semantic_desc and semantic_text and node.text == semantic_text)
    ):
        return node
    prefix = node.path
    for candidate in page.nodes:
        if len(candidate.path) <= len(prefix) or candidate.path[: len(prefix)] != prefix:
            continue
        if semantic_desc and candidate.content_desc == semantic_desc:
            return candidate
        if not semantic_desc and semantic_text and candidate.text == semantic_text:
            return candidate
    return node


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    rendered = []
    for index, part in enumerate(parts):
        if part:
            rendered.append(f"'{part}'")
        if index < len(parts) - 1:
            rendered.append('"\'"')
    return f"concat({', '.join(rendered)})"


def _same_class_ordinal(page: PageModel, node: SemanticNode) -> int:
    siblings = [
        item
        for item in page.nodes
        if item.parent_ordinal == node.parent_ordinal and item.class_name == node.class_name
    ]
    for index, item in enumerate(siblings, start=1):
        if item.ordinal == node.ordinal:
            return index
    return 1


def _relative_bucket(node: SemanticNode, screen_size: Tuple[int, int]) -> str:
    if not node.bounds:
        return "unknown"
    width, height = screen_size
    if width <= 0 or height <= 0:
        return "unknown"
    x1, y1, x2, y2 = node.bounds
    center_x = (x1 + x2) / 2 / width
    center_y = (y1 + y2) / 2 / height
    col = min(2, max(0, int(center_x * 3)))
    row = min(2, max(0, int(center_y * 3)))
    return f"r{row}c{col}"


def _build_constrained_xpath(
    page: PageModel,
    node: SemanticNode,
    *,
    desc: str,
    text: str,
) -> Tuple[str, Optional[SemanticNode], int]:
    anchor = _nearest_stable_ancestor(page, node)
    predicates = [f"@class={_xpath_literal(node.class_name)}"]
    if desc:
        predicates.append(f"@content-desc={_xpath_literal(desc)}")
    elif text:
        predicates.append(f"@text={_xpath_literal(text)}")
    target_expr = f"node[{' and '.join(predicates)}]"

    if anchor is not None:
        if anchor.stable_desc:
            anchor_expr = f"//*[@content-desc={_xpath_literal(anchor.stable_desc)}]"
        else:
            anchor_expr = f"//*[@text={_xpath_literal(anchor.stable_text)}]"
        matching = [
            candidate
            for candidate in page.nodes
            if candidate.class_name == node.class_name
            and candidate.path[: len(anchor.path)] == anchor.path
            and (not desc or candidate.content_desc == desc)
            and (not text or desc or candidate.text == text)
        ]
        ordinal = next(
            (index for index, item in enumerate(matching, start=1) if item.ordinal == node.ordinal),
            1,
        )
        return f"({anchor_expr}/descendant::{target_expr})[{ordinal}]", anchor, ordinal

    matching = [
        candidate
        for candidate in page.nodes
        if candidate.class_name == node.class_name
        and (not desc or candidate.content_desc == desc)
        and (not text or desc or candidate.text == text)
    ]
    ordinal = next(
        (index for index, item in enumerate(matching, start=1) if item.ordinal == node.ordinal),
        1,
    )
    return f"(//{target_expr})[{ordinal}]", None, ordinal


def _build_bounds_xpath(
    page: PageModel,
    node: SemanticNode,
    *,
    desc: str,
    text: str,
) -> Optional[str]:
    """Build a duplicate-safe locator for the current visible control."""
    if node.bounds is None or len(node.bounds) != 4:
        return None
    predicates = [f"@class={_xpath_literal(node.class_name)}"]
    if desc:
        predicates.append(f"@content-desc={_xpath_literal(desc)}")
    elif text:
        predicates.append(f"@text={_xpath_literal(text)}")
    x1, y1, x2, y2 = node.bounds
    bounds_value = f"[{int(x1)},{int(y1)}][{int(x2)},{int(y2)}]"
    predicates.append(f"@bounds={_xpath_literal(bounds_value)}")
    target_expr = f"node[{' and '.join(predicates)}]"
    anchor = _nearest_stable_ancestor(page, node)
    if anchor is not None:
        anchor_expr = (
            f"//*[@content-desc={_xpath_literal(anchor.stable_desc)}]"
            if anchor.stable_desc
            else f"//*[@text={_xpath_literal(anchor.stable_text)}]"
        )
        return f"({anchor_expr}/descendant::{target_expr})[1]"
    return f"(//{target_expr})[1]"


def _direct_bounds_locator(node: SemanticNode) -> Optional[Dict[str, Any]]:
    """Build a fresh-XML-validated locator independent of rotating labels."""
    if node.bounds is None or len(node.bounds) != 4:
        return None
    x1, y1, x2, y2 = node.bounds
    bounds_value = f"[{int(x1)},{int(y1)}][{int(x2)},{int(y2)}]"
    return {
        "selector": (
            f"(//node[@class={_xpath_literal(node.class_name)} and "
            f"@bounds={_xpath_literal(bounds_value)}])[1]"
        ),
        "by": "xpath",
        "expected_class": node.class_name,
        "target_description": None,
        "target_text": None,
        "bounds": list(node.bounds),
        "bounds_constrained": True,
    }


def _compile_custom_patterns(
    safety_rules: Optional[Sequence[Dict[str, Any]]],
) -> List[Tuple[str, re.Pattern[str]]]:
    compiled = [(risk, re.compile(pattern, re.I)) for risk, pattern in _DEFAULT_BLOCK_RULES]
    for rule in safety_rules or ():
        if bool(rule.get("allow")) or str(
            rule.get("risk_type") or ""
        ).strip().upper() == "ALLOW":
            continue
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern:
            continue
        try:
            compiled.append(
                (
                    str(rule.get("risk_type") or "CUSTOM").strip().upper(),
                    re.compile(pattern, re.I),
                )
            )
        except re.error:
            continue
    return compiled


def _custom_action_allowed(
    haystack: str,
    safety_rules: Optional[Sequence[Dict[str, Any]]],
) -> bool:
    for rule in safety_rules or ():
        is_allow = bool(rule.get("allow")) or str(
            rule.get("risk_type") or ""
        ).strip().upper() == "ALLOW"
        if not is_allow:
            continue
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern:
            continue
        try:
            candidates = (haystack, *haystack.split(" | "))
            if any(re.search(pattern, candidate, re.I) for candidate in candidates):
                return True
        except re.error:
            continue
    return False


def _has_strong_haier_cashier_anchor(
    page: PageModel,
    *,
    screen_size: Tuple[int, int],
) -> bool:
    page_package = _normalize_safety_text(page.package_name).casefold()
    if page_package != _HAIER_MALL_PACKAGE:
        return False
    if _HAIER_CASHIER_ACTIVITY_RE.search(_normalize_safety_text(page.activity)):
        return True
    _, screen_height = _page_screen_size(page, screen_size)
    for node in page.nodes:
        if not node.visible:
            continue
        node_package = _normalize_safety_text(node.node_package).casefold()
        if node_package and node_package != page_package:
            continue
        has_cashier_label = any(
            _HAIER_CASHIER_ANCHOR_RE.fullmatch(_normalize_safety_text(value))
            for value in (node.content_desc, node.text)
            if value
        )
        if not has_cashier_label:
            continue
        resource_title = bool(_SAFETY_CONTEXT_RESOURCE_RE.search(node.resource_id))
        top_static_title = bool(
            node.bounds
            and node.bounds[1] <= int(screen_height * 0.18)
            and node.bounds[3] <= int(screen_height * 0.30)
            and not node.clickable
        )
        if resource_title or top_static_title:
            return True
    return False


def _target_subtree_raw_semantics(
    page: PageModel,
    node: SemanticNode,
) -> Tuple[str, ...]:
    """Return bounded in-memory labels for the actionable target subtree."""
    # The subtree is consumed once to find independently actionable children
    # and again to collect bounded text. Materialize the generator so payment
    # labels and other risk evidence are not silently lost on the second pass.
    subtree = list(_subtree_nodes(page, node))
    nested_action_paths = [
        candidate.path
        for candidate in subtree
        if candidate.ordinal != node.ordinal
        and (candidate.clickable or candidate.editable or candidate.scrollable)
    ]
    values: List[str] = []
    seen: set[str] = set()
    total_length = 0
    for candidate in subtree:
        if not candidate.visible:
            continue
        if any(
            len(candidate.path) >= len(action_path)
            and candidate.path[: len(action_path)] == action_path
            for action_path in nested_action_paths
        ):
            # A card/container must not inherit destructive or external risk
            # from an independently actionable child such as "门店电话".
            continue
        for raw_value in (candidate.content_desc, candidate.text):
            value = _normalize_safety_text(raw_value)
            if not value or value in seen:
                continue
            if len(values) >= 64 or total_length + len(value) > 1000:
                return tuple(values)
            seen.add(value)
            values.append(value)
            total_length += len(value)
    return tuple(values)


def _page_safety_context(
    page: PageModel,
    *,
    screen_size: Tuple[int, int],
) -> str:
    """Return bounded title-like context, excluding arbitrary page body copy."""
    _, screen_height = _page_screen_size(page, screen_size)
    page_package = _normalize_safety_text(page.package_name).casefold()
    values = [f"PAGE_ROLE={page.role}"]
    seen = set(values)
    for node in page.nodes:
        if not node.visible:
            continue
        node_package = _normalize_safety_text(node.node_package).casefold()
        if node_package and page_package and node_package != page_package:
            continue
        resource_title = bool(_SAFETY_CONTEXT_RESOURCE_RE.search(node.resource_id))
        top_static_label = bool(
            node.bounds
            and node.bounds[1] <= int(screen_height * 0.18)
            and node.bounds[3] <= int(screen_height * 0.30)
            and not node.clickable
        )
        for raw_value in (node.content_desc, node.text):
            value = _normalize_safety_text(raw_value)
            if not value or len(value) > 120:
                continue
            cashier_title = bool(_HAIER_CASHIER_ANCHOR_RE.fullmatch(value))
            if not (resource_title or top_static_label or cashier_title):
                continue
            if value not in seen:
                seen.add(value)
                values.append(value)
    return " | ".join(values)[:1000]


def classify_risk(
    *,
    node: SemanticNode,
    ancestor_semantics: str,
    page_context: str,
    safety_rules: Optional[Sequence[Dict[str, Any]]] = None,
    effective_description: str = "",
    effective_text: str = "",
    page_package: str = "",
    unlabeled_coordinate_click: bool = False,
    haier_cashier_page: bool = False,
    target_subtree_semantics: Sequence[str] = (),
) -> Tuple[Optional[str], Optional[str]]:
    direct_haystack = " | ".join(
        item
        for item in (
            node.content_desc,
            node.text,
            node.class_name,
            ancestor_semantics,
            effective_description,
            effective_text,
            *target_subtree_semantics,
        )
        if item
    )
    node_package = str(node.node_package or "").strip().lower()
    page_package = str(page_package or "").strip().lower()
    # Any actionable node owned by another package is an external/system
    # surface. Vendor overlays (ColorOS side bar, MIUI floating windows, etc.)
    # must be blocked just like AOSP permission dialogs unless explicitly
    # allow-listed.
    is_system_surface = bool(
        node_package
        and page_package
        and node_package != page_package
    )
    explicitly_allowed = _custom_action_allowed(direct_haystack, safety_rules)
    if is_system_surface and not explicitly_allowed:
        return (
            "SYSTEM_OR_EXTERNAL",
            f"未放行的系统/权限界面: {node.node_package}",
        )
    if explicitly_allowed:
        return None, None
    compiled = _compile_custom_patterns(safety_rules)
    for risk_type, pattern in compiled:
        match = pattern.search(direct_haystack)
        if match:
            # Do not persist the matched UI value: custom patterns may match a
            # token, account identifier or other user data.
            return risk_type, f"安全规则命中: {risk_type}"

    target_label = _normalize_safety_text(
        effective_description
        or effective_text
        or node.content_desc
        or node.text
    ).lower()
    payment_labels = (
        target_label,
        *(_normalize_safety_text(value).lower() for value in target_subtree_semantics),
    )
    if haier_cashier_page and any(
        _is_final_payment_action(value) for value in payment_labels if value
    ):
        return "PAYMENT", "海尔收银台最终付款安全规则命中: PAYMENT"

    # Generic confirmation controls may inherit only bounded title-like
    # context. Pure-coordinate controls are handled separately by
    # COORDINATE_UNSAFE and never inherit arbitrary page copy.
    generic_confirmation = target_label in {
        "确定",
        "确认",
        "继续",
        "提交",
        "完成",
        "ok",
        "confirm",
        "continue",
        "submit",
        "done",
    }
    context_requires_guard = generic_confirmation and not unlabeled_coordinate_click
    if context_requires_guard:
        for risk_type, pattern in compiled:
            match = pattern.search(page_context)
            if match:
                return risk_type, f"页面上下文安全规则命中: {risk_type}"
    return None, None


def coordinate_target_key(
    action: InspectionAction,
) -> Optional[Tuple[int, int]]:
    """Return a resolution-independent coordinate click point for deduplication."""
    if not action.coordinate_only or action.action_type != "click":
        return None
    bounds = action.target_meta.get("bounds")
    source_size = action.target_meta.get("screen_size")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return None
    if not isinstance(source_size, (list, tuple)) or len(source_size) != 2:
        return None
    try:
        source_width, source_height = (int(value) for value in source_size)
        x1, y1, x2, y2 = (int(value) for value in bounds)
    except (TypeError, ValueError):
        return None
    if source_width <= 0 or source_height <= 0 or x2 <= x1 or y2 <= y1:
        return None
    center_x = (x1 + x2) / 2
    center_y = (y1 + y2) / 2
    if not (0 <= center_x < source_width and 0 <= center_y < source_height):
        return None
    # Quantize normalized coordinates so screenshots with equivalent scaled
    # bounds resolve to one physical tap target.
    return (
        round(center_x * 1_000_000 / source_width),
        round(center_y * 1_000_000 / source_height),
    )


def _crop_phash(
    screenshot_png: bytes,
    bounds: Sequence[int],
) -> str:
    if not screenshot_png or len(bounds) != 4:
        return ""
    try:
        with Image.open(io.BytesIO(screenshot_png)) as image:
            x1, y1, x2, y2 = (int(value) for value in bounds)
            x1 = max(0, min(image.width, x1))
            y1 = max(0, min(image.height, y1))
            x2 = max(0, min(image.width, x2))
            y2 = max(0, min(image.height, y2))
            if x2 <= x1 or y2 <= y1:
                return ""
            crop = image.crop((x1, y1, x2, y2)).convert("RGB")
            output = io.BytesIO()
            crop.save(output, format="PNG")
        return perceptual_hash(output.getvalue())
    except (OSError, TypeError, ValueError):
        return ""


def _home_visual_action_evidence(
    page: PageModel,
    *,
    screen_size: Tuple[int, int],
    screenshot_png: bytes,
    navigation_groups: Sequence[NavigationGroup],
    limit: int = 6,
) -> Dict[int, Dict[str, Any]]:
    if page.role != "HOME" or not screenshot_png:
        return {}
    screen_width, screen_height = screen_size
    screen_area = max(1, screen_width * screen_height)
    candidates: List[SemanticNode] = []
    for node in page.nodes:
        if (
            not node.visible
            or not node.enabled
            or not node.clickable
            or node.bounds is None
            or node.semantic
            or _node_is_inside_navigation_member(page, node, navigation_groups)
        ):
            continue
        x1, y1, x2, y2 = node.bounds
        area_ratio = _bounds_area(node.bounds) / screen_area
        if (
            area_ratio < 0.02
            or area_ratio > 0.45
            or y1 < screen_height * 0.03
            or y2 > screen_height * 0.84
        ):
            continue
        descendants = list(_subtree_nodes(page, node))[1:]
        if not any(_class_family(item.class_name) == "image" for item in descendants):
            continue
        if any(item.semantic for item in descendants):
            continue
        subtree_text = " | ".join(
            normalize_semantic_text(value)
            for item in descendants
            for value in (item.content_desc, item.text)
            if normalize_semantic_text(value)
        )
        if _FINAL_PAYMENT_ACTION_RE.search(subtree_text):
            continue
        candidates.append(node)

    selected: List[SemanticNode] = []
    for node in sorted(candidates, key=lambda item: (_bounds_area(item.bounds), item.path)):
        duplicate = False
        for existing in selected:
            intersection = _bounds_intersection_area(node.bounds, existing.bounds)
            smaller = min(_bounds_area(node.bounds), _bounds_area(existing.bounds))
            union = _bounds_area(node.bounds) + _bounds_area(existing.bounds) - intersection
            if smaller and (
                intersection / smaller >= 0.85
                or (union > 0 and intersection / union >= 0.85)
            ):
                duplicate = True
                break
        if duplicate:
            continue
        selected.append(node)
        if len(selected) >= max(0, int(limit)):
            break

    evidence: Dict[int, Dict[str, Any]] = {}
    for node in selected:
        bounds = list(node.bounds or ())
        crop_hash = _crop_phash(screenshot_png, bounds)
        if not crop_hash:
            continue
        evidence[node.ordinal] = {
            "kind": "HOME_IMAGE",
            "source_semantic_key": page.semantic_key,
            "source_page_subtype": page.page_subtype,
            "normalized_bounds": list(_normalized_bounds(node.bounds, screen_size)),
            "crop_phash": crop_hash,
            "max_phash_distance": 8,
        }
    return evidence


def visual_locator_matches(
    action: InspectionAction,
    page: PageModel,
    screenshot_png: bytes,
) -> bool:
    """Validate a HOME visual tile against the current capture before tapping."""
    evidence = (action.target_meta or {}).get("visual_locator")
    if not isinstance(evidence, dict) or evidence.get("kind") != "HOME_IMAGE":
        return False
    if page.role != "HOME" or page.page_subtype != "HOME":
        return False
    expected_semantic = str(evidence.get("source_semantic_key") or "")
    if expected_semantic and expected_semantic != str(page.semantic_key or ""):
        return False
    normalized = evidence.get("normalized_bounds")
    if not isinstance(normalized, (list, tuple)) or len(normalized) != 4:
        return False
    try:
        with Image.open(io.BytesIO(screenshot_png)) as image:
            bounds = [
                round(float(normalized[0]) * image.width),
                round(float(normalized[1]) * image.height),
                round(float(normalized[2]) * image.width),
                round(float(normalized[3]) * image.height),
            ]
    except (OSError, TypeError, ValueError):
        return False
    actual = _crop_phash(screenshot_png, bounds)
    expected = str(evidence.get("crop_phash") or "")
    try:
        threshold = max(0, int(evidence.get("max_phash_distance", 8)))
    except (TypeError, ValueError):
        threshold = 8
    return bool(actual and expected and phash_distance(actual, expected) <= threshold)


def _match_input_rule(
    node: SemanticNode,
    ancestor_semantics: str,
    input_rules: Optional[Sequence[Dict[str, Any]]],
    *,
    page_subtype: str = "",
) -> Optional[Dict[str, Any]]:
    values = {
        "content_desc_regex": node.content_desc,
        "text_regex": node.text,
        "class_regex": node.class_name,
        "ancestor_regex": ancestor_semantics,
        "page_subtype_regex": page_subtype,
    }
    for rule in input_rules or ():
        matched = True
        has_matcher = False
        for key, value in values.items():
            pattern = str(rule.get(key) or "").strip()
            if not pattern:
                continue
            has_matcher = True
            try:
                if re.search(pattern, value or "", re.I) is None:
                    matched = False
                    break
            except re.error:
                matched = False
                break
        if matched and has_matcher:
            return dict(rule)
    return None


def _locator_candidates(
    page: PageModel,
    node: SemanticNode,
    *,
    semantic_desc: str,
    semantic_text: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    candidates: List[Dict[str, Any]] = []
    desc_matches = [
        item
        for item in page.nodes
        if item.visible and item.enabled and item.content_desc == semantic_desc
    ] if semantic_desc else []
    text_matches = [
        item
        for item in page.nodes
        if item.visible and item.enabled and item.text == semantic_text
    ] if semantic_text else []

    if semantic_desc and len(desc_matches) == 1:
        candidates.append(
            {
                "selector": semantic_desc,
                "by": "description",
                "expected_class": node.class_name,
            }
        )
    if semantic_text and len(text_matches) == 1:
        candidates.append(
            {
                "selector": semantic_text,
                "by": "text",
                "expected_class": node.class_name,
            }
        )

    needs_xpath = (
        not candidates
        or (semantic_desc and len(desc_matches) > 1)
        or (semantic_text and len(text_matches) > 1)
    )
    anchor = _nearest_stable_ancestor(page, node)
    if needs_xpath and (semantic_desc or semantic_text or anchor is not None):
        xpath, xpath_anchor, ordinal = _build_constrained_xpath(
            page,
            node,
            desc=semantic_desc,
            text=semantic_text,
        )
        if len(desc_matches) > 1 or len(text_matches) > 1:
            bounds_xpath = _build_bounds_xpath(
                page,
                node,
                desc=semantic_desc,
                text=semantic_text,
            )
            if bounds_xpath:
                candidates.insert(
                    0,
                    {
                        "selector": bounds_xpath,
                        "by": "xpath",
                        "expected_class": node.class_name,
                        "target_description": semantic_desc or None,
                        "target_text": semantic_text if not semantic_desc else None,
                        "bounds": list(node.bounds or ()),
                        "bounds_constrained": True,
                    },
                )
        candidates.append(
            {
                "selector": xpath,
                "by": "xpath",
                "expected_class": node.class_name,
                "target_description": semantic_desc or None,
                "target_text": semantic_text if not semantic_desc else None,
                "anchor_by": (
                    "description"
                    if xpath_anchor and xpath_anchor.stable_desc
                    else "text"
                    if xpath_anchor
                    else None
                ),
                "anchor": (
                    xpath_anchor.stable_desc
                    if xpath_anchor and xpath_anchor.stable_desc
                    else xpath_anchor.stable_text
                    if xpath_anchor
                    else None
                ),
                "ordinal": ordinal,
            }
        )
    return candidates, not candidates


def _overlay_close_action(
    page_subtype: str,
    screen_size: Tuple[int, int],
) -> InspectionAction:
    """Build the single safe action used to leave an overlay."""
    is_filter = page_subtype == "FILTER_PANEL"
    role = "FILTER_CLOSE" if is_filter else "DIALOG_CLOSE"
    display_name = "关闭筛选" if is_filter else "关闭弹窗"
    group_key = _hash_payload(
        {
            "scope": "FAMILY_ACTION",
            "page_subtype": page_subtype,
            "action_role": role,
            "risk": "SAFE",
        }
    )
    return InspectionAction(
        action_type="back",
        action_key=_hash_payload(
            {"type": "back", "page_subtype": page_subtype}
        ),
        locator_candidates=[],
        target_meta={
            "class": "SYSTEM_BACK",
            "content_desc": display_name,
            "text": display_name,
            "bounds": None,
            "screen_size": list(screen_size),
            "enabled": True,
            "checked": False,
            "selected": False,
            "max_repetitions": 1,
            "coordinate_authorized": False,
        },
        coordinate_only=False,
        replayable=True,
        action_role=role,
        action_role_key=_hash_payload(
            {
                "action_role": role,
                "risk_variant": "SAFE",
                "enabled": True,
                "checked": False,
                "selected": False,
            }
        ),
        action_anchor_key=_hash_payload(
            {"action_role": role, "class": "system_back"}
        ),
        action_group_key=group_key,
        action_instance_key=group_key,
        sample_policy="PAGE_ONE",
    )


def enumerate_actions(
    page: PageModel,
    *,
    screen_size: Tuple[int, int],
    screenshot_png: bytes = b"",
    enable_visual_home_actions: bool = False,
    coverage_scheduler_v2: bool = False,
    safety_rules: Optional[Sequence[Dict[str, Any]]] = None,
    input_rules: Optional[Sequence[Dict[str, Any]]] = None,
    max_scrolls_per_direction: int = 3,
    include_current_navigation: bool = False,
) -> List[InspectionAction]:
    """Enumerate deterministic semantic actions for one stable state."""
    resolved_screen_size = _page_screen_size(page, screen_size)
    if coverage_scheduler_v2 and page.page_subtype in {
        "FILTER_PANEL",
        "MODAL_PANEL",
    }:
        return [_overlay_close_action(page.page_subtype, resolved_screen_size)]
    action_nodes: Dict[int, SemanticNode] = {}
    # Text-bearing children of a clickable parent collapse to the parent.
    for node in page.nodes:
        if (
            not node.visible
            or not node.enabled
            or not _node_has_usable_extent(node, resolved_screen_size)
        ):
            continue
        if node.clickable or node.editable or node.scrollable:
            action_nodes[node.ordinal] = node
            continue
        if node.stable_desc or node.stable_text:
            parent = _nearest_clickable_ancestor(page, node)
            if parent is not None and _node_has_usable_extent(
                parent, resolved_screen_size
            ):
                action_nodes[parent.ordinal] = parent

    navigation_by_ordinal: Dict[int, Dict[str, Any]] = {}
    navigation_groups = discover_navigation_groups(
        page, screen_size=resolved_screen_size
    )
    for group in navigation_groups:
        for member in group.members:
            navigation_by_ordinal[member.node_ordinal] = navigation_metadata_for_action(
                page,
                group,
                member,
                all_groups=navigation_groups,
            )

    # Product/service cards often expose both a clickable card and nested
    # clickable labels such as "立即购买". They are one physical transition for
    # coverage purposes. Store cards intentionally retain appointment/phone as
    # separate groups.
    if page.role == "LIST" and page.page_subtype != "STORE_LIST":
        nested_ordinals: set[int] = set()
        for node in action_nodes.values():
            current = page.node(node.parent_ordinal)
            while current is not None:
                if (
                    current.ordinal in action_nodes
                    and current.clickable
                    and _is_collection_card(current, resolved_screen_size)
                ):
                    nested_ordinals.add(node.ordinal)
                    break
                current = page.node(current.parent_ordinal)
        action_nodes = {
            ordinal: node
            for ordinal, node in action_nodes.items()
            if ordinal not in nested_ordinals
        }

    action_nodes = {
        ordinal: node
        for ordinal, node in action_nodes.items()
        if not _node_center_occluded_by_navigation(
            page,
            node,
            navigation_groups,
            screen_size=resolved_screen_size,
        )
    }
    scroll_nodes = _collapse_overlapping_scroll_nodes(
        [node for node in action_nodes.values() if node.scrollable]
    )
    action_nodes = {
        ordinal: node
        for ordinal, node in action_nodes.items()
        if not node.scrollable
    }
    action_nodes.update({node.ordinal: node for node in scroll_nodes})
    unmapped_input_present = any(
        node.editable
        and _match_input_rule(
            node,
            (
                ancestor.stable_desc or ancestor.stable_text
                if (ancestor := _nearest_stable_ancestor(page, node)) is not None
                else ""
            ),
            input_rules,
        )
        is None
        for node in action_nodes.values()
        if node.editable
    )
    visual_evidence_by_ordinal = (
        _home_visual_action_evidence(
            page,
            screen_size=resolved_screen_size,
            screenshot_png=screenshot_png,
            navigation_groups=navigation_groups,
        )
        if enable_visual_home_actions
        else {}
    )

    # Only title-like anchors contribute page context. Product descriptions and
    # other arbitrary body copy must not taint unrelated controls.
    page_context = _page_safety_context(page, screen_size=resolved_screen_size)
    haier_cashier_page = _has_strong_haier_cashier_anchor(
        page,
        screen_size=resolved_screen_size,
    )
    actions: List[InspectionAction] = []
    seen_keys = set()
    coordinate_targets: Dict[Tuple[int, int], int] = {}

    for node in sorted(action_nodes.values(), key=lambda item: item.path):
        product_title_node: Optional[SemanticNode] = None
        if node.scrollable:
            # A scroll locator must identify the container itself. Borrowing a
            # label from a descendant makes the action look replayable while
            # execution still falls back to the container's saved bounds.
            semantic_desc = node.stable_desc
            semantic_text = node.stable_text
            locator_node = node
        else:
            title_desc, title_text, product_title_node = _product_card_title(
                page,
                node,
                screen_size=resolved_screen_size,
            )
            descendant_desc, descendant_text = _descendant_semantics(page, node)
            if product_title_node is not None:
                semantic_desc = title_desc
                semantic_text = title_text
            else:
                semantic_desc = node.stable_desc or descendant_desc
                semantic_text = node.stable_text or descendant_text
            locator_node = _semantic_locator_node(
                page,
                node,
                semantic_desc=semantic_desc,
                semantic_text=semantic_text,
            )
        ancestor = _nearest_stable_ancestor(page, node)
        ancestor_semantics = (
            (ancestor.stable_desc or ancestor.stable_text) if ancestor else ""
        )
        same_class_index = _same_class_ordinal(page, node)
        bucket = _relative_bucket(node, screen_size)

        if node.editable:
            action_type = "input"
        elif node.scrollable:
            action_type = "scroll"
        else:
            action_type = "click"
        if action_type == "scroll" and int(max_scrolls_per_direction) <= 0:
            continue
        navigation = navigation_by_ordinal.get(node.ordinal)
        if action_type == "click" and not include_current_navigation and _navigation_action_is_current(
            page,
            navigation,
            semantic_desc or semantic_text,
        ):
            continue
        if (
            coverage_scheduler_v2
            and page.page_subtype == "CATALOG_CATEGORY"
            and _is_catalog_sidebar_member(page, node)
            and _catalog_sidebar_member_is_selected(
                page,
                node,
                resolved_screen_size,
            )
        ):
            continue

        sensitive_target = bool(
            node.password
            or _SENSITIVE_HINT_RE.search(
                f"{node.content_desc} {node.text} {semantic_desc} "
                f"{semantic_text} {node.class_name}"
            )
        )
        # A password/token field's current value is never a valid stable
        # locator and must not enter paths, transitions, faults or live data.
        locator_text = "" if sensitive_target else semantic_text
        product_locator = _clickable_ancestor_title_locator(
            page,
            node,
            product_title_node,
            title=locator_text,
        )
        if product_locator is not None:
            locator_candidates, coordinate_only = [product_locator], False
        else:
            locator_candidates, coordinate_only = _locator_candidates(
                page,
                locator_node,
                semantic_desc=semantic_desc,
                semantic_text=locator_text,
            )
        visual_evidence = visual_evidence_by_ordinal.get(node.ordinal)
        coordinate_authorized = bool(
            coordinate_only
            and action_type == "click"
            and (
                visual_evidence
                or _custom_action_allowed(
                    " | ".join(
                        item
                        for item in (
                            node.content_desc,
                            node.text,
                            node.class_name,
                            ancestor_semantics,
                        )
                        if item
                    ),
                    safety_rules,
                )
            )
        )
        target_subtree_semantics = (
            _target_subtree_raw_semantics(page, node)
            if action_type == "click"
            else ()
        )
        risk_type, blocked_reason = classify_risk(
            node=node,
            ancestor_semantics=ancestor_semantics,
            page_context=page_context,
            safety_rules=safety_rules,
            effective_description=semantic_desc,
            effective_text=semantic_text,
            page_package=page.package_name,
            unlabeled_coordinate_click=bool(
                action_type == "click"
                and (
                    coordinate_only
                    or not normalize_semantic_text(
                        semantic_desc
                        or semantic_text
                        or node.content_desc
                        or node.text
                    )
                )
            ),
            haier_cashier_page=haier_cashier_page,
            target_subtree_semantics=target_subtree_semantics,
        )
        if (
            action_type == "click"
            and coordinate_only
            and not normalize_semantic_text(semantic_desc or semantic_text)
            and not coordinate_authorized
            and risk_type is None
        ):
            continue
        input_rule = (
            _match_input_rule(
                node,
                ancestor_semantics,
                input_rules,
                page_subtype=page.page_subtype,
            )
            if action_type == "input"
            else None
        )
        if (
            action_type == "input"
            and input_rule is not None
            and str(input_rule.get("id") or "") == "haier_v2_search_keyword"
            and normalize_semantic_text(page.package_name).casefold()
            in _HAIER_MALL_PACKAGES
            and page.page_subtype == "SEARCH"
            and node.bounds is not None
        ):
            # The rotating hot word is the nearest labelled ancestor of the
            # otherwise-unlabelled search field. Revalidate its exact current
            # bounds in fresh XML so that label churn cannot break fixed input.
            bounds_locator = _direct_bounds_locator(node)
            if bounds_locator is not None:
                locator_candidates = [bounds_locator, *locator_candidates]
                coordinate_only = False
        if action_type == "input" and input_rule is None:
            risk_type = risk_type or "UNMAPPED_INPUT"
            blocked_reason = blocked_reason or "输入框未匹配允许的输入规则"

        directions = _scroll_directions(node) if action_type == "scroll" else (None,)
        if action_type == "scroll" and coverage_scheduler_v2:
            # Coverage exploration moves forward through a container. Reverse
            # swipes only restore an already observed viewport and double the
            # device cost without discovering a new action group.
            directions = directions[:1]
        for direction in directions:
            action_role, action_role_key, action_anchor_key = _action_role_for_node(
                page,
                node,
                action_type=action_type,
                semantic=semantic_desc or semantic_text,
                semantic_description=semantic_desc,
                semantic_text=semantic_text,
                screen_size=resolved_screen_size,
                navigation=navigation,
                direction=direction,
            )
            if (
                action_type == "click"
                and action_role == "COMMAND:SEARCH"
                and normalize_semantic_text(page.package_name).casefold()
                in _HAIER_MALL_PACKAGES
            ):
                bounds_locator = _direct_bounds_locator(node)
                if bounds_locator is not None:
                    locator_candidates = [bounds_locator, *locator_candidates]
                    coordinate_only = False
            if (
                action_type == "click"
                and action_role == "ITEM_OPEN:collection"
                and normalize_semantic_text(page.package_name).casefold()
                in _HAIER_MALL_PACKAGES
                and page.page_subtype == "PRODUCT_LIST"
                and _is_haier_product_grid_card(node, resolved_screen_size)
            ):
                # Campaign result cards expose their full product title only on
                # a non-clickable child. Validate the clickable card's current
                # class and bounds in fresh XML, then click that verified card.
                bounds_locator = _direct_bounds_locator(node)
                if bounds_locator is not None:
                    locator_candidates = [bounds_locator, *locator_candidates]
                    coordinate_only = False
            if (
                action_type == "click"
                and action_role in {"OPTION_SELECT", "BUY_NOW"}
                and normalize_semantic_text(page.package_name).casefold()
                in _HAIER_MALL_PACKAGES
                and (
                    page.role == "PRODUCT_DETAIL"
                    or page.page_subtype == "PURCHASE_OPTIONS"
                )
            ):
                bounds_locator = _direct_bounds_locator(node)
                if bounds_locator is not None:
                    locator_candidates = [bounds_locator, *locator_candidates]
                    coordinate_only = False
            if visual_evidence is not None:
                action_role = f"VISUAL_HOME:{_normalized_region(node, resolved_screen_size)}"
                action_role_key = _hash_payload({"action_role": action_role})
                action_anchor_key = _hash_payload(
                    {
                        "action_role": action_role,
                        "bounds": visual_evidence.get("normalized_bounds"),
                    }
                )
            if (
                coverage_scheduler_v2
                and page.role == "PRODUCT_DETAIL"
                and action_role.startswith(_INSTANCE_ACTION_ROLE_PREFIX)
            ):
                continue
            effective_risk_type = risk_type
            effective_blocked_reason = blocked_reason
            if action_role == "STORE_CALL" and not effective_risk_type:
                effective_risk_type = "EXTERNAL_SIDE_EFFECT"
                effective_blocked_reason = "门店电话属于外部系统动作，默认不执行"
            if (
                page.page_subtype == "ADDRESS_FORM"
                and action_role == "COMMAND:SAVE"
            ):
                effective_risk_type = "EXTERNAL_SIDE_EFFECT"
                effective_blocked_reason = "保存收货地址属于数据修改，默认不执行"
            if (
                unmapped_input_present
                and action_role in {"COMMAND:SAVE", "COMMAND:CONFIRM"}
                and not effective_risk_type
            ):
                effective_risk_type = "EXTERNAL_SIDE_EFFECT"
                effective_blocked_reason = (
                    "表单存在未映射输入，保存/确认属于数据修改，默认不执行"
                )
            if not action_role.startswith(_INSTANCE_ACTION_ROLE_PREFIX):
                action_role_key = _hash_payload(
                    {
                        "action_role": action_role,
                        "risk_variant": effective_risk_type or "SAFE",
                        "enabled": node.enabled,
                        "checked": node.checked,
                        "selected": node.selected,
                    }
                )
            action_group_key, action_instance_key, sample_policy = (
                _coverage_action_identity(
                    page,
                    node,
                    action_role=action_role,
                    risk_type=effective_risk_type,
                    screen_size=resolved_screen_size,
                    navigation=navigation,
                    visual_evidence=visual_evidence,
                    semantic=semantic_desc or semantic_text,
                )
            )
            key_payload = {
                "type": action_type,
                "semantic": semantic_desc or semantic_text,
                "class": node.class_name,
                "ancestor": ancestor_semantics,
                "same_class_index": same_class_index,
                "bucket": bucket,
                "direction": direction,
            }
            action_key = _hash_payload(key_payload)
            if action_key in seen_keys:
                continue
            seen_keys.add(action_key)
            target_meta: Dict[str, Any] = {
                "class": node.class_name,
                "content_desc": semantic_desc,
                "text": locator_text,
                "resource_id": node.resource_id,  # passive diagnostics only
                "ancestor_semantic": ancestor_semantics,
                "same_class_index": same_class_index,
                "relative_bucket": bucket,
                "bounds": list(node.bounds) if node.bounds else None,
                "screen_size": list(screen_size),
                "direction": direction,
                "password": sensitive_target,
                "enabled": node.enabled,
                "checked": node.checked,
                "selected": node.selected,
                "max_repetitions": (
                    max_scrolls_per_direction if action_type == "scroll" else 1
                ),
                "coordinate_authorized": coordinate_authorized,
            }
            if product_title_node is not None:
                target_meta["product_title"] = normalize_semantic_text(
                    semantic_text or semantic_desc
                )
            if visual_evidence is not None:
                target_meta["visual_locator"] = dict(visual_evidence)
            if action_type == "click" and navigation is not None:
                target_meta["navigation"] = navigation
            action = InspectionAction(
                action_type=action_type,
                action_key=action_key,
                locator_candidates=locator_candidates,
                target_meta=target_meta,
                coordinate_only=coordinate_only,
                replayable=not coordinate_only and effective_risk_type is None,
                risk_type=effective_risk_type,
                blocked_reason=effective_blocked_reason,
                input_rule_id=str(input_rule.get("id")) if input_rule else None,
                input_variable_key=(
                    str(input_rule.get("variable_key"))
                    if input_rule and input_rule.get("value_source") == "environment"
                    else None
                ),
                action_role=action_role,
                action_role_key=action_role_key,
                action_anchor_key=action_anchor_key,
                action_group_key=action_group_key,
                action_instance_key=action_instance_key,
                sample_policy=sample_policy,
            )
            coordinate_key = coordinate_target_key(action)
            if coordinate_key is not None and coordinate_key in coordinate_targets:
                existing_index = coordinate_targets[coordinate_key]
                existing = actions[existing_index]
                if existing.risk_type is None and action.risk_type is not None:
                    actions[existing_index] = action
                continue
            if coordinate_key is not None:
                coordinate_targets[coordinate_key] = len(actions)
            actions.append(action)
    if coverage_scheduler_v2 and page.page_subtype == "PURCHASE_OPTIONS":
        primary_candidates = [
            action
            for action in actions
            if str(action.action_role or "") in {"BUY_NOW", "ADD_CART"}
        ]
        cleanup = _overlay_close_action(
            page.page_subtype,
            resolved_screen_size,
        )
        if not primary_candidates:
            return [cleanup]
        primary = max(
            primary_candidates,
            key=lambda action: (
                _bounds_area(
                    tuple(action.target_meta.get("bounds") or ())
                    if len(action.target_meta.get("bounds") or ()) == 4
                    else None
                ),
                str(action.action_role or "") == "BUY_NOW",
            ),
        )
        # The current/default option is enough to cover the business
        # transition. Variant chips remain in XML and are not clicked.
        return [primary, cleanup]
    if coverage_scheduler_v2 and page.role == "PRODUCT_DETAIL":
        core_roles = {
            "OPTION_SELECT",
            "BUY_NOW",
            "ADD_CART",
            "FAVORITE",
            "CART_OPEN",
            "ARRIVAL_NOTICE",
            "BACK",
        }
        actions = [
            action
            for action in actions
            if str(action.action_role or "").startswith("SCROLL:vertical:")
            or str(action.action_role or "") in core_roles
        ]
    if coverage_scheduler_v2 and page.page_subtype == "CHECKOUT_CONFIRMATION":
        # The Haier checkout may ask whether optional benefits should be
        # selected. Continue without changing benefits so the existing order
        # can reach the cashier safety boundary deterministically.
        return [
            action
            for action in actions
            if str(action.action_role or "") == "PLACE_ORDER"
        ]
    if coverage_scheduler_v2 and page.page_subtype == "SEARCH":
        fixed_search_enabled = any(
            str(rule.get("id") or "") == "haier_v2_search_keyword"
            for rule in input_rules or ()
            if isinstance(rule, dict)
        )
        if fixed_search_enabled:
            # The v2 Haier contract requires auditable input and submit edges;
            # a volatile hot-word click cannot stand in for that journey.
            actions = [
                action
                for action in actions
                if str(action.action_role or "") in {"INPUT", "SEARCH_SUBMIT"}
            ]
            actions.sort(
                key=lambda action: (
                    0 if str(action.action_role or "") == "INPUT" else 1
                )
            )
        else:
            # Legacy coverage keeps one representative suggestion.
            actions = [
                action
                for action in actions
                if str(action.action_role or "") == "SEARCH_SUGGESTION"
            ]
    if coverage_scheduler_v2 and page.page_subtype == "APPOINTMENT_LIST":
        # Preserve destructive cancellation controls as explicit blocked
        # evidence, but never scroll or attempt to mutate existing bookings.
        actions = [action for action in actions if action.risk_type]
    if coverage_scheduler_v2 and page.page_subtype == "STORE_DETAIL":
        actions = [
            action
            for action in actions
            if str(action.action_role or "")
            in {
                "STORE_APPOINTMENT",
                "STORE_BOOKINGS",
                "STORE_CALL",
                "STORE_PRODUCTS",
            }
            or str(action.action_role or "").startswith("NAV:")
        ]
    if coverage_scheduler_v2 and page.page_subtype == "STORE_LIST":
        # Address/location selectors, promotional chips and list filters only
        # alter presentation. Coverage for this surface is one store card plus
        # the appointment/phone groups; navigation and bounded scrolling remain
        # available for reachability and incremental discovery.
        actions = [
            action
            for action in actions
            if str(action.action_role or "")
            in {"STORE_OPEN", "STORE_APPOINTMENT", "STORE_CALL"}
            or str(action.action_role or "").startswith(("NAV:", "SCROLL:"))
        ]
    if coverage_scheduler_v2 and page.role == "LIST":
        # Horizontal list swipes mostly rotate category chips and banners. The
        # resulting control-state variants are already represented in XML and
        # have repeatedly produced false product-detail States on real devices.
        actions = [
            action
            for action in actions
            if not str(action.action_role or "").startswith(
                "SCROLL:horizontal:"
            )
        ]
    if (
        coverage_scheduler_v2
        and page.page_subtype == "CATALOG_CATEGORY"
        and _catalog_sidebar_members(page.nodes)
    ):
        # On the bottom-level category hub, the full-page RecyclerView and the
        # left rail both expose scrollability even though the right content
        # pane is the only viewport that can reveal a new entry group.
        right_scrolls = [
            action
            for action in actions
            if str(action.action_role or "").startswith("SCROLL:vertical:")
            and isinstance(action.target_meta.get("bounds"), list)
            and len(action.target_meta["bounds"]) == 4
            and action.target_meta["bounds"][0] / max(1, resolved_screen_size[0])
            >= 0.20
        ]
        actions = [
            action
            for action in actions
            if not str(action.action_role or "").startswith("SCROLL:vertical:")
        ] + right_scrolls[:1]
    if coverage_scheduler_v2 and page.page_subtype in {
        "CONSUMABLE_LIST",
        "PRODUCT_LIST",
        "SERVICE_LIST",
    }:
        # These three HOME entries are sampled independently. Their internal
        # category chips only switch presentation variants and must not create
        # another list family after the page's single item/service sample.
        actions = [
            action
            for action in actions
            if not str(action.action_role or "").startswith("CATEGORY_TAB:")
        ]
    if coverage_scheduler_v2 and page.page_subtype == "CASHIER":
        # The cashier is a terminal evidence page for coverage exploration.
        # Preserve the final payment as an explicit blocked edge, but do not
        # spend the task budget switching methods or opening order details.
        return [action for action in actions if action.risk_type == "PAYMENT"]
    if coverage_scheduler_v2 and page.page_subtype == "CART":
        # Cart edit/selection modes multiply states without adding a new
        # business transition once checkout has already been covered through
        # the product-detail primary action.  Preserve the full page evidence
        # and treat this representative as terminal.
        return []
    if coverage_scheduler_v2 and page.page_subtype == "CHECKOUT":
        primary_transitions = [
            action
            for action in actions
            if action.risk_type is None
            and str(action.action_role or "") in {"CHECKOUT", "PLACE_ORDER"}
        ]
        if primary_transitions:
            # Configuration rows can each open another modal and create a
            # combinatorial branch.  Once the checkout CTA is available, the
            # coverage contract is the transition to the cashier; screenshot
            # and XML retain the secondary controls for later regression use.
            return primary_transitions
    return actions


def _locator_matching_nodes(
    xml: str,
    candidate: Dict[str, Any],
) -> List[SemanticNode]:
    try:
        page = build_page_model(xml, package_name="", activity="")
    except ValueError:
        return []
    screen_size = _page_screen_size(page)
    navigation_groups = discover_navigation_groups(page, screen_size=screen_size)

    def eligible(node: SemanticNode) -> bool:
        return bool(
            node.visible
            and node.enabled
            and not _node_center_occluded_by_navigation(
                page,
                node,
                navigation_groups,
                screen_size=screen_size,
            )
        )

    by = str(candidate.get("by") or "").lower()
    selector = str(candidate.get("selector") or "")
    if by in {"description", "desc", "content-desc"}:
        return [
            node
            for node in page.nodes
            if eligible(node) and node.content_desc == selector
        ]
    if by == "text":
        return [
            node
            for node in page.nodes
            if eligible(node) and node.text == selector
        ]
    if by != "xpath":
        return []

    expected_class = str(candidate.get("expected_class") or "")
    expected_desc = str(candidate.get("target_description") or "")
    expected_text = str(candidate.get("target_text") or "")
    expected_descendant_text = normalize_semantic_text(
        candidate.get("target_descendant_text")
    )
    expected_bounds: Optional[Tuple[int, int, int, int]] = None
    if candidate.get("bounds_constrained"):
        raw_bounds = candidate.get("bounds")
        if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
            return []
        try:
            expected_bounds = tuple(int(value) for value in raw_bounds)
        except (TypeError, ValueError):
            return []
    anchor_by = str(candidate.get("anchor_by") or "")
    anchor_value = str(candidate.get("anchor") or "")
    ordinal = int(candidate.get("ordinal") or 1)
    matching: List[SemanticNode] = []
    for node in page.nodes:
        if not eligible(node):
            continue
        if expected_class and node.class_name != expected_class:
            continue
        if expected_bounds is not None and node.bounds != expected_bounds:
            continue
        if expected_desc and node.content_desc != expected_desc:
            continue
        if expected_text and node.text != expected_text:
            continue
        if expected_descendant_text:
            matching_descendants = [
                descendant
                for descendant in _subtree_nodes(page, node)
                if descendant.ordinal != node.ordinal
                and normalize_semantic_text(descendant.text)
                == expected_descendant_text
            ]
            if candidate.get("nearest_clickable_ancestor"):
                has_descendant = any(
                    (nearest := _nearest_clickable_ancestor(page, descendant))
                    is not None
                    and nearest.ordinal == node.ordinal
                    for descendant in matching_descendants
                )
            else:
                has_descendant = bool(matching_descendants)
            if not has_descendant:
                continue
        if anchor_value:
            ancestor = _nearest_stable_ancestor(page, node)
            found = False
            while ancestor is not None:
                value = (
                    ancestor.content_desc if anchor_by == "description" else ancestor.text
                )
                if value == anchor_value:
                    found = True
                    break
                ancestor = page.node(ancestor.parent_ordinal)
            if not found:
                continue
        matching.append(node)
    # An indexed XPath resolves to at most one node, but it is invalid when the
    # expected ordinal drifted outside the current match set.
    return [matching[ordinal - 1]] if 1 <= ordinal <= len(matching) else []


def locator_match_count(xml: str, candidate: Dict[str, Any]) -> int:
    """Count visible+enabled matches before a replay click.

    XPath candidates generated by this module carry enough metadata to audit
    uniqueness without relying on ElementTree's deliberately small XPath
    implementation.
    """
    return len(_locator_matching_nodes(xml, candidate))


def locator_unique_bounds(
    xml: str,
    candidate: Dict[str, Any],
) -> Optional[Tuple[int, int, int, int]]:
    matches = _locator_matching_nodes(xml, candidate)
    if len(matches) != 1:
        return None
    return matches[0].bounds


def locator_quality(actions: Iterable[InspectionAction]) -> str:
    methods = {
        str(candidate.get("by") or "").lower()
        for action in actions
        for candidate in action.locator_candidates
    }
    if "description" in methods:
        return "DESCRIPTION"
    if "text" in methods:
        return "TEXT"
    if "xpath" in methods:
        return "XPATH"
    return "COORDINATE_ONLY"
