"""Tests for the content-insensitive ``surface_key`` identity layer.

The property under test is the one the whole coverage denominator rests on: two
captures of the same screen carrying different records must land on one surface,
and two genuinely different screens must not.
"""

import unittest

from backend.inspection.semantics import (
    SURFACE_FINGERPRINT_VERSION,
    build_page_model,
)

PACKAGE = "com.ehaier.zgq.shop.mall"
ACTIVITY = "com.ehaier.mall.MainActivity"

SCREEN_HEIGHT = 2400
SCREEN_WIDTH = 1080


def _node(
    class_name,
    bounds,
    *,
    desc="",
    text="",
    clickable=False,
    scrollable=False,
    editable=False,
    children="",
):
    return (
        f'<node class="{class_name}" package="{PACKAGE}" '
        f'content-desc="{desc}" text="{text}" resource-id="" '
        f'bounds="{bounds}" clickable="{str(clickable).lower()}" '
        f'scrollable="{str(scrollable).lower()}" '
        f'focusable="false" enabled="true" checkable="false" checked="false" '
        f'selected="false" password="false" long-clickable="false" '
        f'displayed="true"'
        + (
            f' focused="false">{children}</node>'
            if children
            else ' focused="false"/>'
        )
    )


def _editable(bounds, *, desc=""):
    return (
        f'<node class="android.widget.EditText" package="{PACKAGE}" '
        f'content-desc="{desc}" text="" resource-id="" bounds="{bounds}" '
        f'clickable="true" scrollable="false" focusable="true" enabled="true" '
        f'checkable="false" checked="false" selected="false" password="false" '
        f'long-clickable="false" displayed="true" focused="false"/>'
    )


def _page(*body):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<hierarchy rotation="0">'
        f'{_node("android.widget.FrameLayout", f"[0,0][{SCREEN_WIDTH},{SCREEN_HEIGHT}]", children="".join(body))}'
        "</hierarchy>"
    )


def _product_detail(*, promo_modules, with_buy_now=True, price="1999"):
    """A product detail page: fixed chrome, a variable set of promo modules.

    Real products differ by which *kinds* of module they carry - a coupon strip,
    a bundle picker, a spec table - not by repeating one row, so the modules use
    different widget classes here.  Repeated identical rows would already
    collapse inside ``template_tokens``, which is a set.
    """
    module_classes = [
        "android.widget.TextView",
        "android.widget.Button",
        "android.widget.ImageView",
        "android.widget.CheckBox",
        "android.widget.RatingBar",
        "android.widget.ProgressBar",
        "android.widget.Spinner",
    ]
    scroll_children = "".join(
        _node(
            module_classes[index % len(module_classes)],
            f"[0,{400 + index * 120}][{SCREEN_WIDTH},{500 + index * 120}]",
            text=f"促销模块 {index} 省{index * 37}元",
        )
        for index in range(promo_modules)
    ) + _node(
        "android.widget.TextView",
        f"[0,300][{SCREEN_WIDTH},390]",
        text=f"￥{price}",
    )
    bottom = _node(
        "android.widget.Button",
        f"[540,2210][810,{SCREEN_HEIGHT}]",
        desc="加入购物车",
        clickable=True,
    )
    if with_buy_now:
        bottom += _node(
            "android.widget.Button",
            f"[810,2210][{SCREEN_WIDTH},{SCREEN_HEIGHT}]",
            desc="立即购买",
            clickable=True,
        )
    return _page(
        # header
        _node("android.widget.ImageView", "[0,0][120,180]", desc="返回", clickable=True),
        # scrollable body holding the records
        _node(
            "android.widget.ScrollView",
            f"[0,180][{SCREEN_WIDTH},2200]",
            scrollable=True,
            children=scroll_children,
        ),
        # bottom action bar
        bottom,
    )


class SurfaceIdentityTests(unittest.TestCase):
    def _surface(self, xml):
        return build_page_model(
            xml, package_name=PACKAGE, activity=ACTIVITY
        ).surface_key

    def test_same_screen_with_different_records_is_one_surface(self):
        """The bug this fixes: content variation used to mint a new page."""
        few = self._surface(_product_detail(promo_modules=1, price="1999"))
        many = self._surface(_product_detail(promo_modules=7, price="23998"))
        self.assertTrue(few)
        self.assertEqual(few, many)

    def test_template_key_still_separates_the_variants(self):
        """surface_key is additive: the finer identities must keep working."""
        few = build_page_model(
            _product_detail(promo_modules=1),
            package_name=PACKAGE,
            activity=ACTIVITY,
        )
        many = build_page_model(
            _product_detail(promo_modules=7),
            package_name=PACKAGE,
            activity=ACTIVITY,
        )
        self.assertEqual(few.surface_key, many.surface_key)
        self.assertNotEqual(few.template_tokens, many.template_tokens)

    def test_different_bottom_bar_is_a_different_surface(self):
        """A page without 立即购买 does not offer that action.

        Merging it would let coverage claim the buy-now path was checked on a
        page that never had the button.
        """
        with_buy = self._surface(_product_detail(promo_modules=3))
        without_buy = self._surface(
            _product_detail(promo_modules=3, with_buy_now=False)
        )
        self.assertNotEqual(with_buy, without_buy)

    def test_search_header_separates_a_list_from_a_plain_list(self):
        plain = _page(
            _node("android.widget.TextView", "[0,0][400,180]", text="分类"),
            _node(
                "android.widget.ScrollView",
                f"[0,180][{SCREEN_WIDTH},{SCREEN_HEIGHT}]",
                scrollable=True,
                children=_node(
                    "android.widget.TextView", "[0,200][1080,300]", text="冰箱"
                ),
            ),
        )
        searchable = _page(
            _editable("[0,0][900,180]"),
            _node(
                "android.widget.ScrollView",
                f"[0,180][{SCREEN_WIDTH},{SCREEN_HEIGHT}]",
                scrollable=True,
                children=_node(
                    "android.widget.TextView", "[0,200][1080,300]", text="冰箱"
                ),
            ),
        )
        self.assertNotEqual(self._surface(plain), self._surface(searchable))

    def test_surface_key_is_stamped_with_its_rule_version(self):
        page = build_page_model(
            _product_detail(promo_modules=2),
            package_name=PACKAGE,
            activity=ACTIVITY,
        )
        self.assertEqual(
            page.surface_fingerprint_version, SURFACE_FINGERPRINT_VERSION
        )
        self.assertIn("surface_key", page.signature)
        self.assertEqual(page.signature["surface_key"], page.surface_key)

    def test_skeleton_ignores_the_scrolled_record_body(self):
        page = build_page_model(
            _product_detail(promo_modules=6),
            package_name=PACKAGE,
            activity=ACTIVITY,
        )
        # Chrome only: back button, two bottom buttons, one scroll descriptor.
        self.assertLessEqual(len(page.skeleton_tokens), 6)
        self.assertLess(len(page.skeleton_tokens), len(page.template_tokens))


if __name__ == "__main__":
    unittest.main()
