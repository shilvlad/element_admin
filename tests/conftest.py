import os
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

@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client