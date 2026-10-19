FROM python:3.12-slim
WORKDIR /app

# CairoSVG requires the system Cairo shared library at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY assets/ ./assets/
EXPOSE 8001 8002
CMD ["python", "server.py"]
