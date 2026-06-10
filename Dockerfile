# Target: Raspberry Pi (linux/arm64 or linux/arm/v7)
# Build: docker build -t edge-scada .

FROM python:3.11-slim

# gcc + libffi-dev: fallback build tools in case bcrypt has no pre-built wheel for this arch
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping gcc libffi-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/config /app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/status')" || exit 1

CMD ["python", "main.py"]
