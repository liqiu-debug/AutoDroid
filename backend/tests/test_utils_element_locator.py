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

    def test_default_flags_off_still_ignore_id_and_class(self):
        xml = """
        <hierarchy rotation="0">
          <node index="0" text="提交" resource-id="com.demo:id/submit_btn" class="android.widget.Button" content-desc="" clickable="true" bounds="[100,200][400,320]" />
          <node index="1" text="" resource-id="com.demo:id/pure_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[500,200][620,320]" />
        </hierarchy>
        """
        text_res = calculate_element_from_coordinates(xml, 250, 260)
        self.assertEqual(text_res["strategy"], "text")
        self.assertEqual(text_res["selector"], "提交")

        icon_res = calculate_element_from_coordinates(xml, 560, 260)
        self.assertNotIn(icon_res["strategy"], ("id", "class_name", "composite"))

    def test_enable_resource_id_picks_unique_id_for_pure_icon_and_composite_for_shared_id(self):
        xml = """
        <hierarchy rotation="0">
          <node index="0" text="" resource-id="com.demo:id/back_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[20,40][100,120]" />
          <node index="1" text="微信支付" resource-id="com.demo:id/pay_btn" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,300][500,400]" />
          <node index="2" text="支付宝" resource-id="com.demo:id/pay_btn" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,420][500,520]" />
        </hierarchy>
        """
        icon_res = calculate_element_from_coordinates(
            xml,
            60,
            80,
            enable_resource_id=True,
            locator_priority="semantic_first",
        )
        self.assertEqual(icon_res["strategy"], "resourceId")
        self.assertEqual(icon_res["selector"], "com.demo:id/back_icon")

        # 同 ID 不同 text：应合成 id + text 的 composite
        pay_res = calculate_element_from_coordinates(
            xml,
            200,
            460,
            enable_resource_id=True,
            locator_priority="id_first",
        )
        self.assertEqual(pay_res["strategy"], "composite")
        self.assertEqual(
            pay_res["selector"],
            '{"id":"com.demo:id/pay_btn","text":"支付宝"}',
        )

    def test_same_text_different_id_synthesizes_composite_and_respects_priority(self):
        xml = """
        <hierarchy rotation="0">
          <node index="0" text="确认" resource-id="com.demo:id/top_confirm" class="android.widget.Button" content-desc="" clickable="true" bounds="[40,100][240,180]" />
          <node index="1" text="确认" resource-id="com.demo:id/bottom_confirm" class="android.widget.Button" content-desc="" clickable="true" bounds="[40,700][240,780]" />
          <node index="2" text="个人中心" resource-id="com.demo:id/tab_mine" class="android.widget.TextView" content-desc="" clickable="true" bounds="[40,850][240,950]" />
        </hierarchy>
        """
        dup_text_res = calculate_element_from_coordinates(
            xml,
            100,
            740,
            enable_resource_id=True,
            locator_priority="semantic_first",
        )
        self.assertEqual(dup_text_res["strategy"], "composite")
        self.assertEqual(
            dup_text_res["selector"],
            '{"id":"com.demo:id/bottom_confirm","text":"确认"}',
        )

        # id_first 对两者都唯一的元素应优先选 resourceId
        id_first_res = calculate_element_from_coordinates(
            xml,
            100,
            900,
            enable_resource_id=True,
            locator_priority="id_first",
        )
        self.assertEqual(id_first_res["strategy"], "resourceId")
        self.assertEqual(id_first_res["selector"], "com.demo:id/tab_mine")

        # semantic_first 对两者都唯一的元素应优先选 text
        sem_first_res = calculate_element_from_coordinates(
            xml,
            100,
            900,
            enable_resource_id=True,
            locator_priority="semantic_first",
        )
        self.assertEqual(sem_first_res["strategy"], "text")
        self.assertEqual(sem_first_res["selector"], "个人中心")

    def test_noise_id_filtered_and_duplicate_pure_icon_returns_ambiguous(self):
        xml = """
        <hierarchy rotation="0">
          <node index="0" text="" resource-id="android:id/content" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1080,1920]">
            <node index="0" text="" resource-id="com.demo:id/row_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[40,120][140,220]" />
            <node index="1" text="" resource-id="com.demo:id/row_icon" class="android.widget.ImageView" content-desc="" clickable="true" bounds="[40,320][140,420]" />
          </node>
        </hierarchy>
        """
        res = calculate_element_from_coordinates(
            xml,
            90,
            170,
            enable_resource_id=True,
            enable_class_name=True,
        )
        self.assertEqual(res["strategy"], "ambiguous")

    def test_clickable_parent_container_lifts_unique_id_for_plain_icon_child(self):
        xml = """
        <hierarchy rotation="0">
          <node index="0" text="" resource-id="android:id/content" class="android.widget.FrameLayout" content-desc="" clickable="false" bounds="[0,0][1080,1920]">
            <node index="0" text="" resource-id="com.demo:id/close_btn" class="android.widget.LinearLayout" content-desc="" clickable="true" bounds="[900,80][1020,180]">
              <node index="0" text="" resource-id="" class="android.widget.ImageView" content-desc="" clickable="false" bounds="[920,100][1000,160]" />
            </node>
          </node>
        </hierarchy>
        """
        res = calculate_element_from_coordinates(
            xml,
            960,
            130,
            enable_resource_id=True,
        )
        self.assertEqual(res["strategy"], "resourceId")
        self.assertEqual(res["selector"], "com.demo:id/close_btn")

    def test_enable_class_name_uniqueness_and_generic_layout_filtering(self):
        xml = """
        <hierarchy rotation="0">
          <node index="0" text="" resource-id="" class="android.widget.EditText" content-desc="" clickable="true" bounds="[60,200][900,300]" />
          <node index="1" text="设置" resource-id="" class="android.widget.TextView" content-desc="" clickable="true" bounds="[60,400][300,480]" />
          <node index="2" text="设置" resource-id="" class="android.widget.Button" content-desc="" clickable="true" bounds="[60,600][300,680]" />
          <node index="3" text="" resource-id="" class="android.widget.FrameLayout" content-desc="" clickable="true" bounds="[60,800][300,900]" />
        </hierarchy>
        """
        edit_res = calculate_element_from_coordinates(
            xml,
            200,
            250,
            enable_class_name=True,
        )
        self.assertEqual(edit_res["strategy"], "className")
        self.assertEqual(edit_res["selector"], "android.widget.EditText")

        comp_res = calculate_element_from_coordinates(
            xml,
            150,
            640,
            enable_class_name=True,
        )
        self.assertEqual(comp_res["strategy"], "composite")
        self.assertEqual(
            comp_res["selector"],
            '{"class_name":"android.widget.Button","text":"设置"}',
        )

        # FrameLayout 属于通用布局容器，无其他可用属性时应回落 xpath（不应产出 className / composite）
        layout_res = calculate_element_from_coordinates(
            xml,
            150,
            850,
            enable_class_name=True,
        )
        self.assertEqual(layout_res["strategy"], "xpath")

    def test_ios_same_lineage_parent_cell_and_child_text_do_not_conflict(self):
        xml = """
        <AppiumAUT>
          <XCUIElementTypeApplication name="Demo" label="Demo" enabled="true" visible="true" x="0" y="0" width="390" height="844">
            <XCUIElementTypeCell name="订单详情" label="订单详情" enabled="true" visible="true" x="0" y="100" width="390" height="80">
              <XCUIElementTypeStaticText name="订单详情" label="订单详情" enabled="true" visible="true" x="20" y="120" width="200" height="40" />
            </XCUIElementTypeCell>
          </XCUIElementTypeApplication>
        </AppiumAUT>
        """
        res = calculate_element_from_coordinates(
            xml,
            60,
            140,
            enable_resource_id=True,
            locator_priority="id_first",
        )
        self.assertNotIn(res["strategy"], ("ambiguous", "xpath"))
        self.assertEqual(res["selector"], "订单详情")


if __name__ == "__main__":
    unittest.main()
