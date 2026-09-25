"""Deteccao server-side de atualizacoes dos aplicativos oficiais."""

from types import SimpleNamespace

import pytest

from backend.auth import service


class _DeviceResult:
    def __init__(self, device):
        self._device = device

    def scalar_one_or_none(self):
        return self._device


class _DeviceSession:
    def __init__(self, device):
        self.device = device
        self.commits = 0

    def execute(self, _statement):
        return _DeviceResult(self.device)

    def commit(self):
        self.commits += 1


def _device(platform: str, version: str):
    return SimpleNamespace(
        id="device-database-id",
        device_id="stable-device-id",
        public_key="public-key",
        platform=platform,
        app_version=version,
        status=service.DeviceStatus.authorized,
        last_seen=None,
    )


def _info(platform: str, version: str):
    return SimpleNamespace(
        device_id="stable-device-id",
        public_key="public-key",
        platform=platform,
        app_version=version,
    )


@pytest.mark.parametrize("platform", ["windows", "android"])
def test_versao_mais_nova_avisa_uma_vez(monkeypatch, platform):
    avisos = []
    device = _device(platform, "1.5.5")
    db = _DeviceSession(device)
    monkeypatch.setattr(
        service, "notify_app_updated",
        lambda updated_device, previous: avisos.append(
            (updated_device.platform, previous, updated_device.app_version)
        ),
    )

    service._get_or_register_device(db, _info(platform, "1.5.6"), "127.0.0.1")
    service._get_or_register_device(db, _info(platform, "1.5.6"), "127.0.0.1")

    assert avisos == [(platform, "1.5.5", "1.5.6")]
    assert device.app_version == "1.5.6"
    assert db.commits == 2


@pytest.mark.parametrize(
    ("platform", "previous", "current"),
    [
        ("windows", "1.5.6", "1.5.6"),
        ("android", "1.1.7", "1.1.6"),
        ("web", "1.0.0", "1.1.0"),
    ],
)
def test_nao_avisa_sem_atualizacao_dos_apps(monkeypatch, platform, previous, current):
    monkeypatch.setattr(
        service, "notify_app_updated",
        lambda *_args: pytest.fail("nao deveria avisar"),
    )

    service._get_or_register_device(
        _DeviceSession(_device(platform, previous)),
        _info(platform, current),
        "127.0.0.1",
    )
