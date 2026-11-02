import socket
import pytest


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Phase 3 tests are offline only")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
