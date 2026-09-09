from fastapi.testclient import TestClient

from app.db import RegistrationRequest, SessionLocal
from app.main import app


client = TestClient(app)


def test_public_registration_cannot_create_admin(client, monkeypatch):
    monkeypatch.setattr(
        "app.main.send_moderation_notice",
        lambda *args, **kwargs: None,
    )

    response = client.post(
        "/request",
        data={
            "username": "hacker",
            "displayname": "Hacker",
            "requester": "evil@example.com",
            "is_admin": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200

    with SessionLocal() as db:
        request = db.query(RegistrationRequest).order_by(
            RegistrationRequest.id.desc()
        ).first()

        assert request is not None
        assert request.username == "hacker"
        assert request.is_admin is False