from typing import Any, Iterable, List, Mapping, TypeVar


T = TypeVar("T")


_DEVICE_STATUS_ORDER = {
    "BUSY": 0,
    "RUNNING": 0,
    "FASTBOT_RUNNING": 0,
    "IDLE": 1,
    "WDA_DOWN": 2,
    "OFFLINE": 3,
}


def _read_device_value(device: Any, field: str, default: str = "") -> Any:
    if isinstance(device, Mapping):
        return device.get(field, default)
    return getattr(device, field, default)


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def device_sort_key(device: Any) -> tuple:
    status = str(_read_device_value(device, "status", "") or "").strip().upper()
    model = _normalize_text(_read_device_value(device, "model", ""))
    serial = _normalize_text(_read_device_value(device, "serial", ""))
    return (_DEVICE_STATUS_ORDER.get(status, 99), model, serial)


def sort_devices_for_display(devices: Iterable[T]) -> List[T]:
    return sorted(list(devices), key=device_sort_key)
