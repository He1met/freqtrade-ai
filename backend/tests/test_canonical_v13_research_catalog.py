from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.canonical_v13.api import API_PREFIX, create_canonical_v13_app
from app.canonical_v13.genesis import install_canonical_genesis
from app.canonical_v13.manifest import CANONICAL_BUSINESS_SCHEMA
from app.canonical_v13.models import RESEARCH_RUN_CATALOG_TABLE
from app.canonical_v13.research_catalog import (
    ResearchCatalogBlocked,
    ResearchResult,
    apply_research_import,
    get_research_result,
    list_research_results,
    load_research_result,
    plan_research_import,
)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "btc-eth-sol-crowding-15m-v1",
        "name": "BTC ETH SOL derivatives crowding",
        "hypothesis": "Crowding dislocations may mean-revert after costs.",
        "universe": ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"],
        "timeframe": "15m",
        "status": "BOUNDED_NEGATIVE",
        "reason_code": "TRAIN_NO_EDGE_STOP_WITHOUT_VALIDATION_PNL",
        "dataset_digest": "a" * 64,
        "artifact_path": "research/results/crowding/validation.json",
        "result_digest": "b" * 64,
        "train_validation_holdout_summary": {
            "train": "FAILED_PROMOTION",
            "validation": "NOT_EVALUATED",
            "holdout": "SEALED_UNREAD",
        },
        "metrics_summary": {"candidate_count": 6, "qualified_count": 0},
        "created_at": "2026-08-26T17:40:00Z",
    }
    payload.update(overrides)
    return payload


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as raw:
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        install_canonical_genesis(connection, installer_identity="catalog-test")
    return engine


def test_parser_accepts_only_one_exact_research_result_contract(tmp_path: Path) -> None:
    source = tmp_path / "research_result.json"
    source.write_text(json.dumps(_payload()), encoding="utf-8")
    parsed = load_research_result(source)
    assert parsed.run_id == "btc-eth-sol-crowding-15m-v1"
    assert parsed.created_at == datetime(2026, 8, 26, 17, 40, tzinfo=timezone.utc)

    source.write_text(json.dumps(_payload(system_identifier="legacy")), encoding="utf-8")
    with pytest.raises(ResearchCatalogBlocked, match="extra_forbidden"):
        load_research_result(source)
    source.write_text(json.dumps(_payload(status="QUALIFIED")), encoding="utf-8")
    with pytest.raises(ResearchCatalogBlocked, match="qualification_decisions"):
        load_research_result(source)


def test_result_digest_is_the_idempotency_key_and_conflicts_fail_closed() -> None:
    engine = _engine()
    result = ResearchResult.model_validate(_payload())
    try:
        with engine.begin() as raw:
            connection = raw.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            )
            assert plan_research_import(connection, result).action == "INSERT"
            assert apply_research_import(connection, result).action == "INSERTED"
            assert plan_research_import(connection, result).action == "NO_OP"
            assert apply_research_import(connection, result).action == "NO_OP"
            assert list_research_results(connection) == [result]
            assert get_research_result(connection, run_id=result.run_id) == result

            run_conflict = ResearchResult.model_validate(
                _payload(result_digest="c" * 64)
            )
            with pytest.raises(ResearchCatalogBlocked, match="RUN_ID_CONFLICT"):
                plan_research_import(connection, run_conflict)

            digest_conflict = ResearchResult.model_validate(
                _payload(run_id="different-run", name="Different")
            )
            with pytest.raises(ResearchCatalogBlocked, match="DIGEST_CONFLICT"):
                plan_research_import(connection, digest_conflict)
    finally:
        engine.dispose()


def test_list_and_detail_api_are_read_only() -> None:
    engine = _engine()
    result = ResearchResult.model_validate(_payload())
    with engine.begin() as raw:
        connection = raw.execution_options(
            schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
        )
        apply_research_import(connection, result)

    @contextmanager
    def connection_factory():
        with engine.connect() as connection:
            yield connection

    client = TestClient(
        create_canonical_v13_app(
            reader_connection_factory=connection_factory,
            control_connection_factory=connection_factory,
        ),
        raise_server_exceptions=False,
    )
    try:
        catalog = client.get(f"{API_PREFIX}/research-runs")
        assert catalog.status_code == 200
        assert catalog.json()["items"][0]["result_digest"] == "b" * 64
        detail = client.get(f"{API_PREFIX}/research-runs/{result.run_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "BOUNDED_NEGATIVE"
        missing = client.get(f"{API_PREFIX}/research-runs/missing")
        assert missing.status_code == 404

        with engine.connect() as raw:
            connection = raw.execution_options(
                schema_translate_map={CANONICAL_BUSINESS_SCHEMA: None}
            )
            assert connection.execute(
                select(func.count()).select_from(RESEARCH_RUN_CATALOG_TABLE)
            ).scalar_one() == 1
    finally:
        client.close()
        engine.dispose()
