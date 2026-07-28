FROM python:3.12-slim

RUN useradd -m -u 1000 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

USER appuser
ENV PORT=7860
EXPOSE 7860

CMD ["python", "server.py"]
