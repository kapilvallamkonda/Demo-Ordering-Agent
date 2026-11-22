FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY phone_agent ./phone_agent
COPY data/businesses ./data/businesses
COPY data/templates ./data/templates

# The database lives on a mounted volume (see fly.toml), not in the image.
ENV DATABASE_PATH=/data/phone_agent.db \
    CATALOG_PATH=/app/data/businesses/hyderabad-house.json

# Runs as root: Fly volumes mount root-owned, and each Fly machine is its own microVM.
EXPOSE 8000
CMD ["uvicorn", "phone_agent.server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
