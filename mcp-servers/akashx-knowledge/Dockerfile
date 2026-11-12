FROM python:3.12-slim
WORKDIR /app
# fastmcp 2.12 pulls mcp 1.16, which imports a pydantic internal removed in 2.12+ (same pin as akashxdb-mcp)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --default-timeout=120 --retries 10 "fastmcp==2.12.5" "pydantic>=2.11,<2.12" numpy openai
COPY knowledge ./knowledge
COPY data ./data
COPY server.py .
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8000"]
