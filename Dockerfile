FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY gunicorn.conf.py .
COPY llmproxy ./llmproxy

RUN useradd -m appuser
# The audit trail writes here (AUDIT_FILE), and compose bind-mounts ./logs over
# it: the directory must belong to the runtime user, or the writer thread only
# ever counts permission errors.
RUN mkdir -p /app/logs && chown appuser:appuser /app/logs
USER appuser

EXPOSE 11434

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"11434\")}/health')" || exit 1

# Server di produzione (gunicorn con worker threaded, compatibili con lo streaming SSE).
# HOST/PORT e i parametri di concorrenza sono configurabili via env.
#
# Concorrenza: WEB_CONCURRENCY x THREADS e' il numero massimo di richieste
# servite contemporaneamente, ed e' un limite netto. Un proxy LLM passa quasi
# tutto il tempo di una richiesta bloccato in attesa dell'upstream (una
# generazione dura decine di secondi), quindi i thread costano memoria ma quasi
# nessuna CPU: il default 2x8 = 16 richieste in volo saturava con ~16 client
# concorrenti, indipendentemente dalla potenza della macchina. 32 thread per
# worker alzano il tetto a 64 senza cambiare il profilo di CPU.
#
# UPSTREAM_POOL_SIZE segue THREADS (vedi config.load_settings): il pool di
# connessioni keep-alive deve essere grande quanto i thread che lo usano, o
# urllib3 scarta le connessioni in eccesso e ogni richiesta paga un handshake
# TLS nuovo verso l'upstream.
CMD ["sh", "-c", "gunicorn -c gunicorn.conf.py -w ${WEB_CONCURRENCY:-2} -k gthread --threads ${THREADS:-32} -t ${GUNICORN_TIMEOUT:-600} -b ${HOST:-0.0.0.0}:${PORT:-11434} main:app"]
