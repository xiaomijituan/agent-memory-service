from fastapi.testclient import TestClient

from app.main import create_app


def test_health_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AMC_ENV_FILE", str(tmp_path / "absent.env"))
    app = create_app(embedding_mode="off", auth_scheme="none", db_dir=str(tmp_path / "db"))
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        added = client.post(
            "/v1/memories/add",
            json={
                "request_id": "smoke-1",
                "user_id": "user-1",
                "session_id": "session-1",
                "messages": [{"role": "user", "content": "I moved to Hangzhou last spring."}],
            },
        )
        assert added.status_code == 200
        found = client.post("/v1/memories/search", json={"query": "Hangzhou", "user_id": "user-1", "top_k": 5})
        assert found.status_code == 200
        assert found.json()["data"]
