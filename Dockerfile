# ServiceDesk Copilot - B0 skeleton image.
#
# Authored as text and statically checked only. No image is built, pulled or
# started in Gate B0.
#
# Inside the container the process may listen on the container interface; host
# exposure is limited by docker-compose.yml, which publishes 127.0.0.1 only on a
# private service network.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
