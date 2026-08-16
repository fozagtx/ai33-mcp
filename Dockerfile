FROM python:3.12-slim

LABEL org.opencontainers.image.title="ai33-mcp"
LABEL org.opencontainers.image.created="2026-08-16"

RUN useradd -m -u 1000 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py youtube_tools.py ./

USER appuser
ENV PORT=7860
ENV AI33_MCP_BUILD=2026.08.16
EXPOSE 7860

CMD ["python", "server.py"]
