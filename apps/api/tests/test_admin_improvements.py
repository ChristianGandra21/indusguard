"""Acesso administrativo e projeção sem plano, diff, caminhos ou logs privados."""

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from indusguard_api.improvements import ImprovementRecord, ImprovementStore
from indusguard_api.main import create_app
from indusguard_api.settings import Settings

TOKEN = "admin-test-token-" * 3


def test_admin_requires_token_and_returns_only_summary(tmp_path):
    store = ImprovementStore(tmp_path)
    now = datetime.now(UTC)
    record = ImprovementRecord(
        proposal_id=str(uuid4()),
        evaluation_id=str(uuid4()),
        status="pending_review",
        created_at=now,
        updated_at=now,
        base_commit="a" * 40,
        branch="improvement/test",
        worktree="/private/worktree",
        plan={"secret": "private-data"},
    )
    store.save(record)
    app = create_app(
        settings=Settings(
            _env_file=None,
            admin_token=TOKEN,
            improvements_dir=tmp_path,
            trace_jsonl_enabled=False,
        )
    )
    with TestClient(app) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = client.get("/api/v1/admin/improvements", headers=headers)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "no-store"
        response = client.get(
            "/api/v1/admin/improvements", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()[0]["proposal_id"] == record.proposal_id
        for value in ("private-data", "/private/worktree", "tree_sha", '"plan"'):
            assert value not in response.text
        assert client.post("/api/v1/admin/improvements").status_code == 405
        (store.directory(record.proposal_id) / "record.json").write_text("invalid")
        assert (
            client.get(
                "/api/v1/admin/improvements", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 503
        )


def test_admin_disabled_by_default_and_empty_store_is_real_empty(tmp_path):
    settings = Settings(_env_file=None, improvements_dir=tmp_path, trace_jsonl_enabled=False)
    with TestClient(create_app(settings=settings)) as client:
        assert (
            client.get(
                "/api/v1/admin/improvements", headers={"Authorization": f"Bearer {TOKEN}"}
            ).status_code
            == 401
        )
    with TestClient(
        create_app(
            settings=Settings(
                _env_file=None,
                admin_token=TOKEN,
                improvements_dir=tmp_path,
                trace_jsonl_enabled=False,
            )
        )
    ) as client:
        response = client.get(
            "/api/v1/admin/improvements", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert response.status_code == 200
        assert response.json() == []
