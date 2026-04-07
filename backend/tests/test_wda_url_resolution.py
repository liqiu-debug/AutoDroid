import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from backend.cross_platform_execution import check_wda_health, resolve_ios_wda_url
from backend.models import SystemSetting


class WdaUrlResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.session = Session(self.engine)

    def tearDown(self) -> None:
        self.session.close()

    def test_auto_relay_used_when_no_setting(self):
        with patch(
            "backend.cross_platform_execution.wda_relay_manager.ensure_relay",
            return_value=8233,
        ) as ensure_mock:
            resolved = resolve_ios_wda_url(self.session, "ios-1")
        self.assertEqual(resolved, "http://127.0.0.1:8233")
        ensure_mock.assert_called_once_with("ios-1")

    def test_scoped_local_url_reuses_same_port(self):
        self.session.add(
            SystemSetting(
                key="ios_wda_url.ios-1",
                value="http://127.0.0.1:8250",
            )
        )
        self.session.commit()

        with patch(
            "backend.cross_platform_execution.wda_relay_manager.ensure_relay",
            return_value=8250,
        ) as ensure_mock:
            resolved = resolve_ios_wda_url(self.session, "ios-1")
        self.assertEqual(resolved, "http://127.0.0.1:8250")
        ensure_mock.assert_called_once_with("ios-1", preferred_port=8250)

    def test_scoped_local_url_rewrites_when_actual_port_differs(self):
        self.session.add(
            SystemSetting(
                key="ios_wda_url.ios-1",
                value="http://127.0.0.1:8250/wd/hub",
            )
        )
        self.session.commit()

        with patch(
            "backend.cross_platform_execution.wda_relay_manager.ensure_relay",
            return_value=8251,
        ) as ensure_mock:
            resolved = resolve_ios_wda_url(self.session, "ios-1")
        self.assertEqual(resolved, "http://127.0.0.1:8251/wd/hub")
        ensure_mock.assert_called_once_with("ios-1", preferred_port=8250)

    def test_scoped_remote_url_does_not_trigger_local_relay(self):
        self.session.add(
            SystemSetting(
                key="ios_wda_url.ios-1",
                value="http://10.10.10.2:8100",
            )
        )
        self.session.commit()

        with patch("backend.cross_platform_execution.wda_relay_manager.ensure_relay") as ensure_mock:
            resolved = resolve_ios_wda_url(self.session, "ios-1")
        self.assertEqual(resolved, "http://10.10.10.2:8100")
        ensure_mock.assert_not_called()

    def test_scoped_local_url_without_port_gets_auto_port(self):
        self.session.add(
            SystemSetting(
                key="ios_wda_url.ios-1",
                value="http://localhost/wd/hub",
            )
        )
        self.session.commit()

        with patch(
            "backend.cross_platform_execution.wda_relay_manager.ensure_relay",
            return_value=8260,
        ) as ensure_mock:
            resolved = resolve_ios_wda_url(self.session, "ios-1")
        self.assertEqual(resolved, "http://localhost:8260/wd/hub")
        ensure_mock.assert_called_once_with("ios-1", preferred_port=None)

    def test_check_wda_health_raises_p1005_when_unavailable(self):
        with patch("requests.get", side_effect=RuntimeError("connection refused")):
            with self.assertRaises(RuntimeError) as context:
                check_wda_health("http://127.0.0.1:8200")
        self.assertIn("P1005_WDA_UNAVAILABLE", str(context.exception))


if __name__ == "__main__":
    unittest.main()
