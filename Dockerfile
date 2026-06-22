FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# NumPy 2.x wheels need x86-64-v2; many cheap VPS CPUs only support baseline x86-64.
RUN pip install --no-cache-dir "numpy>=1.26.4,<2.0.0" && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "services.gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "30"]
