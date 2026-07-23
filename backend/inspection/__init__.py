"""Android model-based inspection.

The package is intentionally independent from compatibility XML normalization
and from the legacy recorder locator builder.
"""

from .semantics import (
    InspectionAction,
    NavigationConfirmation,
    NavigationGroup,
    NavigationMember,
    PageModel,
    build_page_model,
    confirm_peer_navigation,
    discover_navigation_groups,
    enumerate_actions,
    is_stable_semantic_text,
)

__all__ = [
    "InspectionAction",
    "NavigationConfirmation",
    "NavigationGroup",
    "NavigationMember",
    "PageModel",
    "build_page_model",
    "confirm_peer_navigation",
    "discover_navigation_groups",
    "enumerate_actions",
    "is_stable_semantic_text",
]
