import unittest

from backend.utils import calculate_element_from_coordinates


class ElementLocatorTests(unittest.TestCase):
    def test_dynamic_time_range_description_falls_back_to_xpath(self):
        """动态时间区间描述应降级为 xpath 兜底（不再自动裁切图像）"""
        xml = """
        <hierarchy>
            <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
                <node class="android.view.View" content-desc="[00:00, 03:55]" bounds="[100,100][980,260]">
                    <node class="com.horcrux.svg.PathView" bounds="[180,140][220,180]" />
                </node>
            </node>
        </hierarchy>
        """
        result = calculate_element_from_coordinates(
            hierarchy_xml=xml,
            target_x=200,
            target_y=160,
        )

        self.assertEqual(result["strategy"], "xpath")
        self.assertIn("PathView", result["selector"])

    def test_stable_description_still_uses_description_selector(self):
        xml = """
        <hierarchy>
            <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
                <node class="android.view.View" content-desc="确认" bounds="[100,100][980,260]">
                    <node class="com.horcrux.svg.PathView" bounds="[180,140][220,180]" />
                </node>
            </node>
        </hierarchy>
        """
        result = calculate_element_from_coordinates(
            hierarchy_xml=xml,
            target_x=200,
            target_y=160,
        )

        self.assertEqual(result["strategy"], "description")
        self.assertEqual(result["selector"], "确认")

    def test_ios_label_uses_text_strategy(self):
        xml = """
        <AppiumAUT>
            <XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="1179" height="2556">
                <XCUIElementTypeButton
                    type="XCUIElementTypeButton"
                    name="login_button"
                    label="登录"
                    x="100"
                    y="240"
                    width="220"
                    height="88"
                />
            </XCUIElementTypeApplication>
        </AppiumAUT>
        """

        result = calculate_element_from_coordinates(
            hierarchy_xml=xml,
            target_x=160,
            target_y=280,
        )

        self.assertEqual(result["strategy"], "text")
        self.assertEqual(result["selector"], "登录")
        self.assertEqual(result["element"]["className"], "XCUIElementTypeButton")

    def test_ios_name_falls_back_to_description_strategy(self):
        xml = """
        <AppiumAUT>
            <XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="1179" height="2556">
                <XCUIElementTypeButton
                    type="XCUIElementTypeButton"
                    name="settings_button"
                    label=""
                    x="880"
                    y="120"
                    width="96"
                    height="96"
                />
            </XCUIElementTypeApplication>
        </AppiumAUT>
        """

        result = calculate_element_from_coordinates(
            hierarchy_xml=xml,
            target_x=920,
            target_y=160,
        )

        self.assertEqual(result["strategy"], "description")
        self.assertEqual(result["selector"], "settings_button")

    def test_ios_physical_pixel_coordinates_respect_scale(self):
        xml = """
        <AppiumAUT>
            <XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="393" height="852">
                <XCUIElementTypeButton
                    type="XCUIElementTypeButton"
                    name="login_button"
                    label="登录"
                    x="100"
                    y="240"
                    width="220"
                    height="88"
                />
            </XCUIElementTypeApplication>
        </AppiumAUT>
        """

        result = calculate_element_from_coordinates(
            hierarchy_xml=xml,
            target_x=480,
            target_y=800,
            coordinate_scale=3.0,
        )

        self.assertEqual(result["strategy"], "text")
        self.assertEqual(result["selector"], "登录")
        self.assertEqual(result["element"]["bounds"], "[300,720][960,984]")


if __name__ == "__main__":
    unittest.main()
