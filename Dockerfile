FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# Playwright "chrome" channel isn't supported on Linux ARM64.
# - x86_64: install real Chrome channel (best for avoiding 403 fingerprinting)
# - arm64: fall back to Playwright Chromium
RUN set -e; arch="$(uname -m)"; \
    if [ "$arch" = "x86_64" ]; then \
      python -m playwright install chrome; \
    else \
      python -m playwright install chromium; \
    fi

COPY src /app/src

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
ENV STUDENTAID_MONARCH_RUNTIME=docker

ENTRYPOINT ["python", "-m", "studentaid_monarch_sync"]


