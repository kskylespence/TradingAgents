"""Foundation smoke tests for the backend.

These confirm only that the skeleton imports, the FastAPI app starts,
Pydantic schemas round-trip, and the DB models map cleanly. Heavier
end-to-end tests live alongside the routers/services they exercise.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter


def test_app_starts_and_health_returns_200() -> None:
    """The real /api/health endpoint responds 200 with the documented shape.

    The placeholder that used to live in main.py was removed because
    FastAPI matches routes in first-registration order; leaving the
    placeholder there would silently shadow the real router and have
    Coolify report "ok" even on DB-down.
    """
    from app.main import app

    assert app.title == "TradingAgents Web UI"

    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    # Real handler always sets status (ok | degraded); db key always present.
    assert body["status"] in {"ok", "degraded"}
    assert body["db"] in {"ok", "down"}
    # No more placeholder marker — the real router superseded it.
    assert "placeholder" not in body


def test_bootstrap_health_alias() -> None:
    """The dedicated bootstrap path exists too, for use after task #4 lands."""
    from app.main import app

    with TestClient(app) as client:
        resp = client.get("/api/_bootstrap_health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_db_models_import_and_register_on_metadata() -> None:
    """Every ORM table is registered on Base.metadata."""
    from app.db import Base
    from app import models  # noqa: F401

    expected = {"runs", "run_events", "api_keys", "user_defaults", "login_attempts"}
    found = set(Base.metadata.tables.keys())
    missing = expected - found
    assert not missing, f"Missing tables in Base.metadata: {missing}"


def test_schemas_round_trip_through_json() -> None:
    """Every shape we promise the frontend round-trips through json.

    Goes Pydantic → JSON string → dict → Pydantic and checks the discriminator
    union resolves the right concrete event type.
    """
    from app import schemas as S

    # RunRequest
    req = S.RunRequest(
        ticker="SPY",
        analysis_date=date(2026, 5, 19),
        output_language="English",
        analysts=["market", "news"],
        research_depth=1,
        llm_provider="openai",
        quick_think_llm="gpt-4o-mini",
        deep_think_llm="gpt-4o",
        openai_reasoning_effort="medium",
        enable_checkpoint=True,
    )
    j = req.model_dump_json()
    parsed = S.RunRequest.model_validate_json(j)
    assert parsed == req

    # RunDetail with optional fields
    detail = S.RunDetail(
        id=uuid.uuid4(),
        ticker="SPY",
        asset_type="stock",
        analysis_date=date(2026, 5, 19),
        status="completed",
        rating="Buy",
        llm_provider="openai",
        research_depth=1,
        started_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 19, 12, 5, tzinfo=timezone.utc),
        created_at=datetime(2026, 5, 19, 12, tzinfo=timezone.utc),
        elapsed_seconds=300.0,
        analysts=["market", "news"],
        quick_think_llm="gpt-4o-mini",
        deep_think_llm="gpt-4o",
        thinking_config=S.ThinkingConfig(openai_reasoning_effort="medium"),
        output_language="English",
        checkpoint_enabled=True,
        decision_full="…",
        report_dir="/data/tradingagents/reports/SPY_2026-05-19",
        error_message=None,
        stats=S.RunStats(llm_calls=10, tokens_in=1000, tokens_out=500, elapsed_seconds=300.0),
        resumable=False,
    )
    j = detail.model_dump_json()
    parsed = S.RunDetail.model_validate_json(j)
    assert parsed == detail

    # RunEvent discriminated union — every concrete type resolves correctly.
    adapter: TypeAdapter = TypeAdapter(S.RunEvent)
    sample_events: list[dict] = [
        {
            "seq": 1,
            "type": "run_started",
            "ticker": "SPY",
            "asset_type": "stock",
            "analysis_date": "2026-05-19",
            "analysts": ["market", "news"],
            "research_depth": 1,
            "llm_provider": "openai",
            "quick_think_llm": "gpt-4o-mini",
            "deep_think_llm": "gpt-4o",
            "output_language": "English",
            "checkpoint_enabled": True,
            "thinking_config": None,
        },
        {"seq": 2, "type": "agent_status", "agent": "Market Analyst", "status": "in_progress"},
        {"seq": 3, "type": "progress_update", "progress": 0.25, "step": "Market Analyst"},
        {"seq": 4, "type": "analyst_wall_time", "key": "market", "label": "Market Analyst", "seconds": 12.3},
        {"seq": 5, "type": "tool_call", "name": "yfinance", "args": {"ticker": "SPY"}, "timestamp": "12:00:01"},
        {"seq": 6, "type": "message", "kind": "Agent", "content": "hello", "timestamp": "12:00:02"},
        {"seq": 7, "type": "report_section", "section": "market_report", "content": "# Market"},
        {"seq": 8, "type": "investment_debate", "bull": "buy", "bear": "sell", "judge": "buy"},
        {"seq": 9, "type": "risk_debate", "aggressive": "max", "judge": "moderate"},
        {"seq": 10, "type": "stats", "llm_calls": 5, "tool_calls": 3, "tokens_in": 100, "tokens_out": 50, "elapsed_seconds": 30.0},
        {"seq": 11, "type": "run_completed", "rating": "Buy", "report_dir": "/data/...", "finished_at": "2026-05-19T12:05:00Z"},
        {"seq": 12, "type": "run_failed", "error": "boom"},
        {"seq": 13, "type": "run_cancelled", "at_node": "Trader"},
    ]
    expected_types = {
        "run_started": S.RunStartedEvent,
        "agent_status": S.AgentStatusEvent,
        "progress_update": S.ProgressUpdateEvent,
        "analyst_wall_time": S.AnalystWallTimeEvent,
        "tool_call": S.ToolCallEvent,
        "message": S.MessageEvent,
        "report_section": S.ReportSectionEvent,
        "investment_debate": S.InvestmentDebateEvent,
        "risk_debate": S.RiskDebateEvent,
        "stats": S.StatsEvent,
        "run_completed": S.RunCompletedEvent,
        "run_failed": S.RunFailedEvent,
        "run_cancelled": S.RunCancelledEvent,
    }
    for raw in sample_events:
        parsed = adapter.validate_python(raw)
        assert isinstance(parsed, expected_types[raw["type"]]), (
            f"{raw['type']!r} resolved to {type(parsed).__name__}"
        )
        # round-trip via JSON
        re_serialized = adapter.dump_json(parsed)
        re_parsed = adapter.validate_json(re_serialized)
        assert type(re_parsed) is type(parsed)


def test_rating_enum_is_exactly_five_tier() -> None:
    """5-tier rating, source-of-truth: tradingagents/agents/utils/rating.py."""
    from typing import get_args

    from app.schemas import Rating

    assert set(get_args(Rating)) == {"Buy", "Overweight", "Hold", "Underweight", "Sell"}


def test_message_event_uses_kind_not_type() -> None:
    """The frontend uses `kind` for the message sub-classification. So do we."""
    from app.schemas import MessageEvent

    fields = set(MessageEvent.model_fields.keys())
    assert "kind" in fields, fields
    # The discriminator on the union is `type`; the inner sub-class uses `kind`.


def test_crypto_round_trip() -> None:
    """encrypt/decrypt round-trip with a generated Fernet key in env."""
    from app import crypto

    crypto.reset_cache()
    secret = "sk-test-12345"
    ct = crypto.encrypt(secret)
    assert isinstance(ct, bytes) and ct != secret.encode()
    pt = crypto.decrypt(ct)
    assert pt == secret


def test_router_registry_starts_empty_and_accepts_registrations() -> None:
    """Downstream agents will call register() to plug their routers in."""
    from fastapi import APIRouter

    from app.routers import ROUTERS, register

    starting = len(ROUTERS)
    r = APIRouter(prefix="/foo")
    register(r)
    try:
        assert ROUTERS[-1] is r
        assert len(ROUTERS) == starting + 1
    finally:
        ROUTERS.pop()


def test_settings_requires_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings must refuse to construct when JWT_SECRET is unset."""
    from pydantic import ValidationError

    from app.config import Settings, get_settings

    monkeypatch.delenv("JWT_SECRET", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()


def test_settings_requires_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings must refuse to construct when FERNET_KEY is unset."""
    from pydantic import ValidationError

    from app.config import Settings, get_settings

    monkeypatch.delenv("FERNET_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()


def test_settings_requires_admin_password_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings must refuse to construct when ADMIN_PASSWORD_HASH is unset."""
    from pydantic import ValidationError

    from app.config import Settings, get_settings

    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        Settings()
