import json
import sys
from types import SimpleNamespace

import pytest

from elice_knowledge import __main__ as cli


@pytest.mark.parametrize(
    ("admin", "secret", "reason"),
    [
        (None, "secret", "admin_access_code_required"),
        ("admin", None, "auth_session_secret_required"),
    ],
)
def test_production_init_requires_admin_before_database(monkeypatch, capsys, admin, secret, reason):
    monkeypatch.setattr(sys, "argv", ["knowledge", "init-db"])
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            app_env="prod", admin_access_code=admin, auth_session_secret=secret
        ),
    )
    called = []
    monkeypatch.setattr(cli, "initialize_database", lambda settings: called.append(settings))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["reason"] == reason
    assert not called


def test_production_init_registers_configured_admin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["knowledge", "init-db"])
    settings = SimpleNamespace(
        app_env="prod", admin_access_code="admin", auth_session_secret="secret"
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    called = []
    monkeypatch.setattr(cli, "initialize_database", lambda value: called.append(value))
    cli.main()
    assert called == [settings]
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
