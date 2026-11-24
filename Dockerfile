FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app app
COPY bin bin
COPY alembic alembic
COPY alembic.ini .
EXPOSE 8000
CMD ["sh", "-ec", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
