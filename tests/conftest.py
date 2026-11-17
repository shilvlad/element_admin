import os
import re
import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine, init_db
from app.main import app

@pytest.fixture(autouse=True)
def database():
    init_db()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield

class CSRFClient(TestClient):
    def post(self, url, **kwargs):
        page = super().get('/login')
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        headers = dict(kwargs.pop('headers', {}) or {})
        headers.setdefault('X-CSRF-Token', token)
        return super().post(url, headers=headers, **kwargs)


@pytest.fixture
def client():
    with CSRFClient(app) as client:
        yield client