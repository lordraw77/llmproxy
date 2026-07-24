#!/usr/bin/env bash
# Demo scriptata di llmproxy per la registrazione asciinema -> GIF.
# Avvia il proxy, mostra il banner e alcune chiamate reali agli endpoint
# Ollama / OpenAI, poi si ferma.
set -u
cd /opt/llmproxy

BASE="http://localhost:11434"
MODEL="meta/llama-3.1-8b-instruct"
GREEN=$'\033[32m'; CYAN=$'\033[36m'; YEL=$'\033[33m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RST=$'\033[0m'

# Stampa un prompt e "digita" il comando carattere per carattere, poi lo esegue.
run() {
  printf '%s$%s ' "$GREEN" "$RST"
  local cmd="$1"
  for ((i=0; i<${#cmd}; i++)); do printf '%s' "${cmd:$i:1}"; sleep 0.010; done
  printf '\n'
  sleep 0.25
  eval "$cmd"
  echo
  sleep 0.6
}
say() { printf '\n%s# %s%s\n' "$CYAN" "$1" "$RST"; sleep 0.5; }

clear
printf '%s%s  llmproxy%s  —  un proxy Ollama / OpenAI-compatibile verso NVIDIA\n' "$BOLD" "$YEL" "$RST"
sleep 0.6

say "Avvio del proxy (dev server, porta 11434)"
printf '%s$%s python3 main.py &\n' "$GREEN" "$RST"; sleep 0.3
python3 main.py >/tmp/llmproxy-run.log 2>&1 &
PROXY_PID=$!
trap 'kill -9 $PROXY_PID 2>/dev/null' EXIT
for _ in $(seq 1 60); do curl -sf "$BASE/health" >/dev/null 2>&1 && break; sleep 0.5; done
sed -n '2,10p' /tmp/llmproxy-run.log   # banner
sleep 1.0

say "Health check"
run "curl -s $BASE/health | jq"

say "Modelli esposti — endpoint nativo Ollama /api/tags"
run "curl -s $BASE/api/tags | jq '.models[:3] | map(.name)'"

say "Chat OpenAI-compatibile — POST /v1/chat/completions"
run "curl -s $BASE/v1/chat/completions -H 'Content-Type: application/json' -d '{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Saluta in 4 parole\"}],\"max_tokens\":40}' | jq -r '.choices[0].message.content'"

say "Streaming nativo Ollama — POST /api/chat (NDJSON, stream=true)"
run "curl -s -N $BASE/api/chat -d '{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Conta da 1 a 5\"}],\"stream\":true}' | jq -rj 'select(.message).message.content'; echo"

printf '\n%s✓ Un solo processo: endpoint Ollama + OpenAI + llama.cpp, streaming e retry.%s\n' "$BOLD" "$RST"
sleep 1.8
