FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY WEBHOOKS.md ./
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["sh", "-c", "mkdir -p data && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
