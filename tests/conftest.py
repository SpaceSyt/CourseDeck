import pytest
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def loopback_test_clients(monkeypatch):
    """Model the desktop socket peer; remote-security tests set their own client IP."""
    original = TestClient.__init__

    def initialize(self, *args, **kwargs):
        kwargs.setdefault("client", ("127.0.0.1", 50000))
        original(self, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", initialize)


@pytest.fixture(autouse=True)
def forbid_real_os_vault(monkeypatch):
    # Synthetic browser sessions must never leave credentials in the user's vault.
    def blocked():
        pytest.fail("Unit test attempted to select the real OS credential backend", pytrace=False)

    monkeypatch.setattr("keyring.get_keyring", blocked)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
