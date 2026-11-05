"""Fastbot 设备清单：桌面 USB 登记的 iOS 真机按 include_ios 透出，App 形态 / 非客户端 iOS 不透出。"""
import pytest
from fastapi import HTTPException

from backend.api import fastbot
from backend.models import Device


def _dev(**kw):
    base = dict(serial="S1", platform="android", status="IDLE")
    base.update(kw)
    return Device(**base)


UDID = "00008120-00146CA4110BC01E"


def test_android_always_visible():
    assert fastbot._fastbot_device_visible(_dev(), include_ios=False)


def test_desktop_usb_ios_visible_only_with_flag():
    ios = _dev(serial=UDID, platform="ios", connection_type="usb", client_instance_id="inst-1")
    assert not fastbot._fastbot_device_visible(ios, include_ios=False)
    assert fastbot._fastbot_device_visible(ios, include_ios=True)


def test_app_form_and_server_scanned_ios_hidden():
    app_form = _dev(serial="ios:ABCD", platform="ios", connection_type="wda", client_instance_id="x")
    server_usb = _dev(serial=UDID, platform="ios", connection_type="usb")
    assert not fastbot._fastbot_device_visible(app_form, include_ios=True)
    assert not fastbot._fastbot_device_visible(server_usb, include_ios=True)


def test_cloud_run_rejects_ios_with_desktop_hint():
    ios = _dev(serial=UDID, platform="ios", connection_type="usb", client_instance_id="inst-1")
    with pytest.raises(HTTPException) as exc:
        fastbot._ensure_fastbot_android_device(ios)
    assert "桌面客户端" in exc.value.detail
