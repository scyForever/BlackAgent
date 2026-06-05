FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY config ./config
COPY scripts ./scripts
COPY src ./src
COPY storage ./storage

RUN pip install --no-cache-dir .

EXPOSE 8765

CMD ["python", "scripts/serve_demo_api.py", "--host", "0.0.0.0", "--port", "8765"]
