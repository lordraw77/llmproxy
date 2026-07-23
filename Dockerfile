FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN useradd -m appuser
USER appuser

EXPOSE 11434

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"11434\")}/health')" || exit 1

# Server di produzione (gunicorn con worker threaded, compatibili con lo streaming SSE).
# HOST/PORT e i parametri di concorrenza sono configurabili via env.
CMD ["sh", "-c", "gunicorn -w ${WEB_CONCURRENCY:-2} -k gthread --threads ${THREADS:-8} -t ${GUNICORN_TIMEOUT:-600} -b ${HOST:-0.0.0.0}:${PORT:-11434} main:app"]
