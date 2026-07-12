from datetime import datetime, timedelta, timezone
from os import utime

from app.core.config import Settings
from app.db.migrations import SchemaReadiness
from app.services.research_readiness import ResearchReadinessService


FIXED_NOW = datetime(2026, 7, 12, 3, 0, tzinfo=timezone.utc)


def make_settings(tmp_path) -> Settings:
    strategy_dir = tmp_path / "strategies"
    market_dir = tmp_path / "market-data"
    artifact_dir = tmp_path / "backtests"
    strategy_dir.mkdir()
    market_dir.mkdir()
    artifact_dir.mkdir()
    (strategy_dir / "LocalResearch.py").write_text("class LocalResearch: pass\n", encoding="utf-8")
    (market_dir / "BTC_USDT-15m.json").write_text("{}", encoding="utf-8")
    (artifact_dir / "result.json").write_text("{}", encoding="utf-8")
    return Settings(
        database_url="postgresql+psycopg://research@localhost:5432/research",
        strategy_output_dir=strategy_dir,
        market_data_dir=str(market_dir),
        backtest_result_dir=str(artifact_dir),
    )


def ready_schema(*_args) -> SchemaReadiness:
    return SchemaReadiness("postgresql://localhost/research", "20260712_01", True, ())


def test_research_readiness_is_ready_only_with_all_local_evidence(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr("app.services.research_readiness.verify_schema", ready_schema)
    service = ResearchReadinessService(
        settings=settings,
        environ={"DEEPSEEK_API_KEY": "present-only-not-rendered"},
        now_provider=lambda: FIXED_NOW,
        which=lambda name: "/opt/local/bin/freqtrade" if name == "freqtrade" else None,
    )

    report = service.build()

    assert report.status == "READY"
    assert "present-only-not-rendered" not in report.model_dump_json()


def test_research_readiness_reports_stale_data_without_dry_run_or_live_inputs(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    stale_file = settings.market_data_dir / "BTC_USDT-15m.json"
    stale_timestamp = (FIXED_NOW - timedelta(days=8)).timestamp()
    utime(stale_file, (stale_timestamp, stale_timestamp))
    monkeypatch.setattr("app.services.research_readiness.verify_schema", ready_schema)
    report = ResearchReadinessService(
        settings=settings,
        environ={"DEEPSEEK_API_KEY": "present"},
        now_provider=lambda: FIXED_NOW,
        which=lambda _name: "/opt/local/bin/freqtrade",
    ).build()

    assert report.status == "STALE"
    assert report.stale_reason is not None


def test_research_readiness_blocks_no_trading_and_marks_missing_artifact_unavailable(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    for artifact in settings.backtest_result_dir.iterdir():
        artifact.unlink()
    monkeypatch.setattr("app.services.research_readiness.verify_schema", ready_schema)
    report = ResearchReadinessService(
        settings=settings,
        environ={"DEEPSEEK_API_KEY": "present"},
        now_provider=lambda: FIXED_NOW,
        which=lambda _name: "/opt/local/bin/freqtrade",
    ).build()

    assert report.status == "UNAVAILABLE"
    assert "backtest artifacts" in (report.unavailable_reason or "")


def test_research_readiness_blocks_corrupt_artifact(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    (settings.backtest_result_dir / "result.json").write_text("not-json", encoding="utf-8")
    monkeypatch.setattr("app.services.research_readiness.verify_schema", ready_schema)
    report = ResearchReadinessService(
        settings=settings,
        environ={"DEEPSEEK_API_KEY": "present"},
        now_provider=lambda: FIXED_NOW,
        which=lambda _name: "/opt/local/bin/freqtrade",
    ).build()

    assert report.status == "BLOCKED"
    assert "not UTF-8 JSON" in (report.blocked_reason or "")
