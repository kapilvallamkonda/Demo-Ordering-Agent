FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY ordering_agent ./ordering_agent
COPY data/menu.json ./data/menu.json

# Orders live on a mounted volume (see fly.toml), not in the image.
ENV ORDERS_DB=/data/orders.db \
    MENU_PATH=/app/data/menu.json

# Runs as root: Fly volumes mount root-owned, and each Fly machine is its own microVM.
EXPOSE 8000
CMD ["uvicorn", "ordering_agent.server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
