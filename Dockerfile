FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBTBOT_DB=/data/debtbot.db

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /data

# The database lives on a persistent volume mounted at /data.
CMD ["python", "-m", "debtbot.bot"]
