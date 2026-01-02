FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Needed for pip git+https dependencies (monarchmoney).
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src /app/src

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

ENTRYPOINT ["python", "-m", "studentaid_monarch_sync"]


