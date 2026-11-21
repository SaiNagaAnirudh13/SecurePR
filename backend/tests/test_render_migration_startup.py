from pathlib import Path

import run


def test_render_installs_requirements_and_migrates_in_startup_wrapper():
    render_config = Path(__file__).resolve().parents[2] / "render.yaml"
    configuration = render_config.read_text(encoding="utf-8")

    assert "buildCommand: pip install -r requirements.txt" in configuration
    assert "preDeployCommand:" not in configuration
    assert "startCommand: python run.py" in configuration
    assert "AUTO_MERGE_ENABLED" not in configuration
    assert "AUTO_MERGE_LOW_RISK_ONLY" not in configuration


def test_production_migration_finishes_before_uvicorn_starts(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "get_settings", lambda: object())
    monkeypatch.setattr(run, "is_production_runtime", lambda settings: True)
    monkeypatch.setattr(
        run, "_upgrade_production_schema", lambda: calls.append("migrate")
    )
    monkeypatch.setattr(
        run.uvicorn, "run", lambda *args, **kwargs: calls.append("serve")
    )

    run.main()

    assert calls == ["migrate", "serve"]
