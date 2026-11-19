import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from app import main


def test_health_returns_ok_only_when_database_is_reachable(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}", future=True)
    monkeypatch.setattr(main, "get_engine", lambda: engine)

    assert main.health()["status"] == "ok"

    def database_unavailable():
        raise RuntimeError("database configuration missing")

    monkeypatch.setattr(main, "get_engine", database_unavailable)
    with pytest.raises(HTTPException) as unavailable:
        main.health()
    assert unavailable.value.status_code == 503
    engine.dispose()
