
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY app ./app
ARG BUILD_COMMIT=unknown
ARG BUILD_DATE=unknown
ENV BUILD_COMMIT=$BUILD_COMMIT
ENV BUILD_DATE=$BUILD_DATE
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
