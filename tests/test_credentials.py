import pytest
from keyring.errors import PasswordDeleteError

from coursedeck.credentials import CredentialStore


class MemoryVault:
    def __init__(self):
        self.data = {}
        self.fail = False

    def get_password(self, service, name):
        return self.data.get((service, name))

    def set_password(self, service, name, value):
        if self.fail and len(value) == 1000:
            raise RuntimeError("Synthetic write failure")
        assert len(value.encode("utf-16-le")) <= 2560
        self.data[service, name] = value

    def delete_password(self, service, name):
        if self.data.pop((service, name), None) is None:
            raise PasswordDeleteError()


def test_large_tokens_and_rotation_are_atomic(tmp_path):
    vault = MemoryVault()
    store = CredentialStore(tmp_path)
    store.backend = lambda: vault
    store.set("token", {"value": "small"})
    vault.fail = True
    with pytest.raises(RuntimeError):
        store.set("token", {"value": "x" * 8000})
    assert store.get("token") == {"value": "small"}
    vault.fail = False
    store.set("token", {"value": "x" * 8000})
    assert store.get("token") == {"value": "x" * 8000}
    store.delete("token")
    assert not vault.data
