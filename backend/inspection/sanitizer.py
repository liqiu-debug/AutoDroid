"""Inspection-only in-memory artifact sanitization."""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageFilter

from backend.inspection.semantics import parse_bounds


_SENSITIVE_HINT_RE = re.compile(
    r"密码|口令|验证码|身份证|银行卡|手机号|token|secret|password|passwd|pin|otp|"
    r"credit\s*card|bank\s*card|phone",
    re.I,
)
_MASK_RE = re.compile(r"^[*•●·xX_\-\s]+$")


def _node_text(element: ET.Element) -> str:
    return " ".join(
        str(element.attrib.get(key) or "")
        for key in ("content-desc", "contentDescription", "description", "text", "value", "class")
    ).strip()


def _matches_rule(element: ET.Element, rule: Dict[str, Any]) -> bool:
    values = {
        "content_desc_regex": str(
            element.attrib.get("content-desc")
            or element.attrib.get("contentDescription")
            or ""
        ),
        "text_regex": str(element.attrib.get("text") or element.attrib.get("value") or ""),
        "class_regex": str(element.attrib.get("class") or ""),
    }
    has_matcher = False
    for key, value in values.items():
        pattern = str(rule.get(key) or "").strip()
        if not pattern:
            continue
        has_matcher = True
        try:
            if re.search(pattern, value, re.I) is None:
                return False
        except re.error:
            return False
    return has_matcher


def _is_sensitive(
    element: ET.Element,
    rules: Optional[Sequence[Dict[str, Any]]],
) -> bool:
    if str(element.attrib.get("password") or "").lower() == "true":
        return True
    if _SENSITIVE_HINT_RE.search(_node_text(element)):
        return True
    text_value = str(element.attrib.get("text") or element.attrib.get("value") or "").strip()
    if text_value and _MASK_RE.fullmatch(text_value):
        return True
    return any(_matches_rule(element, rule) for rule in rules or ())


def _merge_regions(
    regions: Iterable[Tuple[int, int, int, int]]
) -> List[Tuple[int, int, int, int]]:
    # A conservative merge prevents repeated blur artifacts while keeping the
    # implementation deterministic.
    result: List[Tuple[int, int, int, int]] = []
    for candidate in regions:
        x1, y1, x2, y2 = candidate
        merged = False
        for index, current in enumerate(result):
            cx1, cy1, cx2, cy2 = current
            if x2 < cx1 or cx2 < x1 or y2 < cy1 or cy2 < y1:
                continue
            result[index] = (
                min(x1, cx1),
                min(y1, cy1),
                max(x2, cx2),
                max(y2, cy2),
            )
            merged = True
            break
        if not merged:
            result.append(candidate)
    return result


@dataclass(frozen=True)
class SanitizedArtifacts:
    xml: str
    screenshot_png: bytes
    sensitive_regions: List[Tuple[int, int, int, int]]
    screenshot_asset_id: Optional[str] = None
    xml_asset_id: Optional[str] = None


class InspectionArtifactSanitizer:
    """Never writes a raw XML or raw screenshot to disk."""

    def __init__(self, rules: Optional[Sequence[Dict[str, Any]]] = None) -> None:
        self.rules = [dict(item) for item in rules or ()]

    def sanitize(self, xml: str, screenshot_png: bytes) -> SanitizedArtifacts:
        try:
            root = ET.fromstring(str(xml or ""))
        except ET.ParseError as exc:
            raise ValueError(f"invalid hierarchy XML: {exc}") from exc

        regions: List[Tuple[int, int, int, int]] = []
        for element in root.iter():
            if not _is_sensitive(element, self.rules):
                continue
            bounds = parse_bounds(element.attrib.get("bounds"))
            if bounds:
                regions.append(bounds)
            for key in (
                "text",
                "value",
                "content-desc",
                "contentDescription",
                "description",
                "hint",
            ):
                if key in element.attrib:
                    element.attrib[key] = ""

        sanitized_xml = ET.tostring(root, encoding="unicode")
        merged_regions = _merge_regions(regions)
        sanitized_png = self._blur(screenshot_png, merged_regions)
        return SanitizedArtifacts(
            xml=sanitized_xml,
            screenshot_png=sanitized_png,
            sensitive_regions=merged_regions,
        )

    @staticmethod
    def _blur(
        screenshot_png: bytes,
        regions: Sequence[Tuple[int, int, int, int]],
    ) -> bytes:
        if not screenshot_png:
            return b""
        with Image.open(io.BytesIO(screenshot_png)) as source:
            image = source.convert("RGB")
            width, height = image.size
            for x1, y1, x2, y2 in regions:
                box = (
                    max(0, min(width, x1)),
                    max(0, min(height, y1)),
                    max(0, min(width, x2)),
                    max(0, min(height, y2)),
                )
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                crop = image.crop(box).filter(ImageFilter.GaussianBlur(radius=18))
                image.paste(crop, box)
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()

    def write(
        self,
        *,
        xml: str,
        screenshot_png: bytes,
        xml_path: Path,
        screenshot_path: Path,
    ) -> SanitizedArtifacts:
        artifacts = self.sanitize(xml, screenshot_png)
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(artifacts.xml, encoding="utf-8")
        screenshot_path.write_bytes(artifacts.screenshot_png)
        # CAS rollout is deliberately best-effort and feature-gated.  Legacy
        # files are already durable at this point, so an asset-store outage can
        # never make an inspection capture fail.
        from backend.artifact_store import mirror_image, mirror_xml

        screenshot_asset_id = mirror_image(screenshot_path, artifacts.screenshot_png)
        xml_asset_id = mirror_xml(xml_path, artifacts.xml)
        return SanitizedArtifacts(
            xml=artifacts.xml,
            screenshot_png=artifacts.screenshot_png,
            sensitive_regions=artifacts.sensitive_regions,
            screenshot_asset_id=screenshot_asset_id,
            xml_asset_id=xml_asset_id,
        )
