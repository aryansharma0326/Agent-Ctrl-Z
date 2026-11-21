"""OWNER: Visista. Tests for google_tools/. Run:  pytest tests/test_google_tools.py

These never touch real Google: every test forces the fake calendar unless it is
checking the switch itself (and the real backend is only placeholders for now).
"""
import pytest

from fake_data import fake_google
from google_tools import backend, google_api


@pytest.fixture(autouse=True)
def use_fake(monkeypatch):
    monkeypatch.delenv("USE_REAL_GOOGLE", raising=False)


# ---------- step 2: the backend switch ----------
def test_fake_calendar_is_the_default():
    assert backend.get_backend() is fake_google
    assert backend.backend_name() == "fake calendar"


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_flag_switches_to_real_google(monkeypatch, value):
    monkeypatch.setenv("USE_REAL_GOOGLE", value)
    assert backend.get_backend() is google_api
    assert backend.backend_name() == "real Google"


@pytest.mark.parametrize("value", ["0", "", "false"])
def test_other_flag_values_stay_on_fake(monkeypatch, value):
    monkeypatch.setenv("USE_REAL_GOOGLE", value)
    assert backend.get_backend() is fake_google


def test_both_backends_have_every_required_function():
    for module in (fake_google, google_api):
        for name in backend.REQUIRED:
            assert callable(getattr(module, name, None)), f"{module.__name__}.{name} missing"


def test_missing_function_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(backend, "FAKE", "json")  # a module with none of the functions
    with pytest.raises(RuntimeError, match="missing: get_event"):
        backend.get_backend()


def test_real_placeholders_fail_loudly(monkeypatch):
    monkeypatch.setenv("USE_REAL_GOOGLE", "1")
    with pytest.raises(NotImplementedError, match="step 8"):
        backend.get_backend().get_event("evt_demo")