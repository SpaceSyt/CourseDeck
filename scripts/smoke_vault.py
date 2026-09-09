"""Writes and removes a synthetic record to verify the actual OS vault."""

from pathlib import Path
from uuid import uuid4

from coursedeck.credentials import CredentialStore

store = CredentialStore(Path("data"))
name = f"diagnostic-{uuid4().hex}"
try:
    store.set(name, {"synthetic": "x" * 5000})
    assert store.get(name) == {"synthetic": "x" * 5000}
    print(
        f"OS credential store verified: {type(store.backend()).__name__}; large tokens supported."
    )
finally:
    store.delete(name)
