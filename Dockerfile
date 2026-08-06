
FROM python:3.13-slim

WORKDIR /app

# Install deps first so source edits don't invalidate the layer.
COPY pyproject.toml README.md ./
COPY noalpha/ ./noalpha/
RUN pip install --no-cache-dir -e ".[quant]"

# Runtime state (SQLite, model artifacts, cached bars) lives here.
VOLUME /app/.cache

EXPOSE 8000
CMD ["uvicorn", "noalpha.api:app", "--host", "0.0.0.0", "--port", "8000"]
