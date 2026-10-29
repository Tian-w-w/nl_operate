FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY evaluation/ ./evaluation/
COPY data/ ./data/
COPY alembic.ini ./
COPY --from=frontend /build/dist ./frontend/dist/
RUN useradd --create-home llm4web && mkdir -p /app/runtime && chown -R llm4web:llm4web /app
USER llm4web
ENV AUTH_MODE=disabled MODEL_ADAPTER=rules MODEL_CALLS_ENABLED=false PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
