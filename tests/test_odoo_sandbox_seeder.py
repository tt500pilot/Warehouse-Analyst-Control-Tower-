import pytest

from scripts.seed_odoo_sandbox import assert_write_guard


def test_write_guard_allows_dry_run_for_any_database(monkeypatch):
    monkeypatch.delenv("AWIA_ALLOW_SANDBOX_WRITES", raising=False)
    assert_write_guard("scm_os_demo", apply=False)


def test_write_guard_refuses_non_sandbox_database(monkeypatch):
    monkeypatch.setenv("AWIA_ALLOW_SANDBOX_WRITES", "true")
    with pytest.raises(RuntimeError, match="Refusing AWIA sandbox writes"):
        assert_write_guard("scm_os_demo", apply=True)


def test_write_guard_requires_explicit_environment_opt_in(monkeypatch):
    monkeypatch.delenv("AWIA_ALLOW_SANDBOX_WRITES", raising=False)
    with pytest.raises(RuntimeError, match="AWIA_ALLOW_SANDBOX_WRITES=true"):
        assert_write_guard("awia_mock", apply=True)


def test_write_guard_accepts_explicit_sandbox_opt_in(monkeypatch):
    monkeypatch.setenv("AWIA_ALLOW_SANDBOX_WRITES", "true")
    assert_write_guard("awia_mock", apply=True)
