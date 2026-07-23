#!/usr/bin/env bash
#
# Runner dei test per llmproxy.
#
# Uso:
#   ./scripts/tests.sh                 menù interattivo (TUI se disponibile, altrimenti bash)
#   ./scripts/tests.sh <n>             esegue direttamente il test numero <n>
#   ./scripts/tests.sh all             esegue tutti i modelli (ping)
#   ./scripts/tests.sh --list          elenca i test disponibili
#   ./scripts/tests.sh --no-tui        forza il menù bash semplice
#
# Variabili d'ambiente:
#   BASE   URL di llmproxy (default: http://localhost:11434)
#   MODEL  modello di default per i test che ne usano uno
#
set -u

BASE="${BASE:-http://localhost:11434}"
DEFAULT_MODEL="${MODEL:-meta/llama-3.1-8b-instruct}"
USE_TUI=1

# ----------------------------------------------------------------------------
# Utility
# ----------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

# jq e' opzionale: se manca, si stampa il JSON grezzo.
pp() { if have jq; then jq "$@"; else cat; fi; }

hr() { printf '%s\n' "------------------------------------------------------------"; }

req_chat() {
  # req_chat <model> <prompt> [stream]
  local model="$1" prompt="$2" stream="${3:-false}"
  curl -sN "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$model\",\"stream\":$stream,\"messages\":[{\"role\":\"user\",\"content\":$(json_str "$prompt")}]}"
}

# Codifica una stringa in JSON in modo sicuro (usa jq se c'e', altrimenti fallback basilare).
json_str() {
  if have jq; then jq -Rn --arg s "$1" '$s'; else printf '"%s"' "${1//\"/\\\"}"; fi
}

# Recupera l'elenco dei modelli esposti da llmproxy.
list_models() {
  curl -s "$BASE/v1/models" | pp -r '.data[].id' 2>/dev/null
}

# Sceglie un modello: TUI (fzf) se disponibile, altrimenti menù numerato, altrimenti default.
pick_model() {
  local models
  models=$(list_models)
  if [ -z "$models" ]; then
    echo "$DEFAULT_MODEL"
    return
  fi

  if [ "$USE_TUI" = 1 ] && have fzf; then
    echo "$models" | fzf --prompt="Modello> " --height=40% --reverse || echo "$DEFAULT_MODEL"
    return
  fi

  # Menù numerato in bash puro (stderr, cosi' lo stdout resta il nome scelto).
  local arr=() i=1
  while IFS= read -r m; do arr+=("$m"); done <<< "$models"
  {
    echo "Scegli un modello:"
    for m in "${arr[@]}"; do printf "  %2d) %s\n" "$i" "$m"; i=$((i+1)); done
    printf "Numero [1]: "
  } >&2
  local n; read -r n
  n="${n:-1}"
  if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#arr[@]}" ]; then
    echo "${arr[$((n-1))]}"
  else
    echo "$DEFAULT_MODEL"
  fi
}

# ----------------------------------------------------------------------------
# Definizione dei test
# ----------------------------------------------------------------------------

TEST_NAMES=(
  "Discovery: /api/tags e /v1/models"
  "Health e root (+ /health?upstream=1)"
  "Chat OpenAI (scegli modello)"
  "Chat OpenAI in streaming (scegli modello)"
  "Chat Ollama /api/chat (scegli modello)"
  "Generate Ollama /api/generate (scegli modello)"
  "Completion legacy /v1/completions (scegli modello)"
  "Completion llama.cpp /completion (scegli modello)"
  "Vision (immagine, scegli modello vision)"
  "Traduzione (riva-translate)"
  "Content safety (nemotron safety)"
  "Embeddings OpenAI /v1/embeddings"
  "Embeddings Ollama /api/embed e /api/embeddings"
  "Dettaglio modello /v1/models/<id>"
  "Autenticazione (PROXY_API_KEY)"
  "Errore propagato (modello inesistente upstream)"
  "PING di TUTTI i modelli"
)

test_1_discovery() {
  hr; echo "GET /api/tags"; hr
  curl -s "$BASE/api/tags" | pp '.models[].name'
  hr; echo "GET /v1/models"; hr
  curl -s "$BASE/v1/models" | pp '.data[].id'
}

test_2_health() {
  hr; echo "GET /"; hr;        curl -s "$BASE/"; echo
  hr; echo "GET /health"; hr;  curl -s "$BASE/health" | pp .
  hr; echo "GET /health?upstream=1 (verifica raggiungibilita' NVIDIA)"; hr
  curl -s "$BASE/health?upstream=1" | pp .
  hr; echo "GET /api/version"; hr; curl -s "$BASE/api/version" | pp .
  hr; echo "GET /props"; hr;   curl -s "$BASE/props" | pp .
}

test_3_chat() {
  local m; m=$(pick_model)
  hr; echo "Chat OpenAI -> $m"; hr
  req_chat "$m" "Say hello in one short sentence." false | pp -r '.choices[0].message.content // .'
}

test_4_chat_stream() {
  local m; m=$(pick_model)
  hr; echo "Chat OpenAI streaming -> $m"; hr
  req_chat "$m" "Count from 1 to 5." true
}

test_5_ollama_chat() {
  local m; m=$(pick_model)
  hr; echo "Ollama /api/chat -> $m"; hr
  curl -s "$BASE/api/chat" \
    -d "{\"model\":\"$m\",\"stream\":false,\"messages\":[{\"role\":\"user\",\"content\":\"Give me one fun fact about GPUs.\"}]}" \
    | pp -r '.message.content // .'
}

test_6_ollama_generate() {
  local m; m=$(pick_model)
  hr; echo "Ollama /api/generate -> $m"; hr
  curl -s "$BASE/api/generate" \
    -d "{\"model\":\"$m\",\"stream\":false,\"system\":\"You are concise.\",\"prompt\":\"Write a haiku about GPUs.\"}" \
    | pp -r '.response // .'
}

test_7_v1_completions() {
  local m; m=$(pick_model)
  hr; echo "/v1/completions -> $m"; hr
  curl -s "$BASE/v1/completions" \
    -d "{\"model\":\"$m\",\"prompt\":\"The capital of France is\"}" \
    | pp -r '.choices[0].text // .'
}

test_8_llama_completion() {
  local m; m=$(pick_model)
  hr; echo "/completion (llama.cpp) -> $m"; hr
  curl -s "$BASE/completion" \
    -d "{\"model\":\"$m\",\"prompt\":\"The capital of Italy is\"}" \
    | pp -r '.content // .'
}

test_9_vision() {
  local m="${MODEL:-meta/llama-3.2-11b-vision-instruct}"
  local img="https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/240px-Cat03.jpg"
  hr; echo "Vision -> $m"; hr
  curl -s "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"What is in this image?\"},{\"type\":\"image_url\",\"image_url\":{\"url\":\"$img\"}}]}]}" \
    | pp -r '.choices[0].message.content // .'
}

test_10_translate() {
  local m="nvidia/riva-translate-4b-instruct-v1.1"
  hr; echo "Traduzione -> $m"; hr
  req_chat "$m" "Translate to Italian: The weather is nice today." false | pp -r '.choices[0].message.content // .'
}

test_11_safety() {
  local m="nvidia/nemotron-3-content-safety"
  hr; echo "Content safety -> $m"; hr
  req_chat "$m" "How do I bake chocolate chip cookies?" false | pp -r '.choices[0].message.content // .'
}

test_12_embeddings_openai() {
  local m="${EMBED_MODEL:-}"
  hr; echo "Embeddings OpenAI /v1/embeddings${m:+ -> $m}"; hr
  curl -s "$BASE/v1/embeddings" \
    -H "Content-Type: application/json" \
    -d "{${m:+\"model\":\"$m\",}\"input\":\"Hello embeddings world.\"}" \
    | pp -r 'if .data then "model=\(.model)  dim=\(.data[0].embedding | length)  primi5=\(.data[0].embedding[0:5])" else . end'
}

test_13_embeddings_ollama() {
  local m="${EMBED_MODEL:-}"
  hr; echo "Embeddings Ollama /api/embed (nuovo)${m:+ -> $m}"; hr
  curl -s "$BASE/api/embed" \
    -H "Content-Type: application/json" \
    -d "{${m:+\"model\":\"$m\",}\"input\":\"Hello from /api/embed.\"}" \
    | pp -r 'if .embeddings then "model=\(.model)  vettori=\(.embeddings | length)  dim=\(.embeddings[0] | length)" else . end'
  hr; echo "Embeddings Ollama /api/embeddings (legacy)${m:+ -> $m}"; hr
  curl -s "$BASE/api/embeddings" \
    -H "Content-Type: application/json" \
    -d "{${m:+\"model\":\"$m\",}\"prompt\":\"Hello from /api/embeddings.\"}" \
    | pp -r 'if .embedding then "dim=\(.embedding | length)  primi5=\(.embedding[0:5])" else . end'
}

test_14_model_detail() {
  local m; m=$(pick_model)
  hr; echo "Dettaglio modello -> GET /v1/models/$m"; hr
  curl -s -w '\nHTTP %{http_code}\n' "$BASE/v1/models/$m" | pp .
  hr; echo "Modello inesistente -> atteso 404"; hr
  curl -s -w '\nHTTP %{http_code}\n' "$BASE/v1/models/nvidia/does-not-exist" | pp .
}

test_15_auth() {
  hr; echo "Autenticazione (PROXY_API_KEY)"; hr
  echo "Nota: se PROXY_API_KEY non e' impostata lato server, il proxy e' aperto"
  echo "      e tutte le richieste rispondono 200 (comportamento storico)."
  echo "      Imposta PROXY_KEY nell'ambiente del test per inviare il token."
  echo
  echo "-> senza token:"
  curl -s -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/v1/models"
  if [ -n "${PROXY_KEY:-}" ]; then
    echo "-> con Authorization: Bearer <PROXY_KEY>:"
    curl -s -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/v1/models" \
      -H "Authorization: Bearer $PROXY_KEY"
    echo "-> con X-Api-Key:"
    curl -s -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/v1/models" \
      -H "X-Api-Key: $PROXY_KEY"
    echo "-> con token errato (atteso 401 se auth attiva):"
    curl -s -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/v1/models" \
      -H "Authorization: Bearer chiave-sbagliata"
  fi
  echo "-> /health (sempre esente da auth):"
  curl -s -o /dev/null -w 'HTTP %{http_code}\n' "$BASE/health"
}

test_16_error() {
  hr; echo "Errore propagato -> modello inesistente"; hr
  curl -s -w '\nHTTP %{http_code}\n' "$BASE/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model":"nvidia/does-not-exist","messages":[{"role":"user","content":"hi"}]}'
}

test_17_all() {
  local models pass=0 fail=0 content err code resp body
  models=$(list_models)
  [ -z "$models" ] && { echo "Nessun modello su $BASE/v1/models"; return 1; }
  while IFS= read -r model; do
    [ -z "$model" ] && continue
    printf '%-55s ' "$model"
    resp=$(curl -s -w '\n%{http_code}' "$BASE/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with just: OK\"}],\"max_tokens\":16}")
    code=$(printf '%s' "$resp" | tail -n1)
    body=$(printf '%s' "$resp" | sed '$d')
    if [ "$code" = "200" ]; then
      content=$(printf '%s' "$body" | pp -r '.choices[0].message.content // empty' | tr '\n' ' ')
      echo "✅ ${content:0:60}"; pass=$((pass+1))
    else
      err=$(printf '%s' "$body" | pp -r '.error.message // .error // empty' 2>/dev/null | tr '\n' ' ')
      echo "❌ [$code] ${err:0:60}"; fail=$((fail+1))
    fi
  done <<< "$models"
  echo; echo "OK: $pass   FALLITI: $fail"
}

# Mappa numero -> funzione
run_test() {
  case "$1" in
    1)  test_1_discovery ;;
    2)  test_2_health ;;
    3)  test_3_chat ;;
    4)  test_4_chat_stream ;;
    5)  test_5_ollama_chat ;;
    6)  test_6_ollama_generate ;;
    7)  test_7_v1_completions ;;
    8)  test_8_llama_completion ;;
    9)  test_9_vision ;;
    10) test_10_translate ;;
    11) test_11_safety ;;
    12) test_12_embeddings_openai ;;
    13) test_13_embeddings_ollama ;;
    14) test_14_model_detail ;;
    15) test_15_auth ;;
    16) test_16_error ;;
    17|all) test_17_all ;;
    *)  echo "Test sconosciuto: $1" >&2; return 1 ;;
  esac
}

list_tests() {
  local i=1
  for name in "${TEST_NAMES[@]}"; do printf "%2d) %s\n" "$i" "$name"; i=$((i+1)); done
}

# ----------------------------------------------------------------------------
# Menù
# ----------------------------------------------------------------------------

menu_tui_whiptail() {
  local args=() i=1 choice bin
  bin=$(have whiptail && echo whiptail || echo dialog)
  for name in "${TEST_NAMES[@]}"; do args+=("$i" "$name"); i=$((i+1)); done
  while true; do
    choice=$("$bin" --title "llmproxy tests ($BASE)" \
      --menu "Scegli un test da eseguire:" 25 76 17 \
      "${args[@]}" 3>&1 1>&2 2>&3) || break
    clear
    run_test "$choice"
    echo; read -rp "Premi INVIO per tornare al menù..." _
  done
}

menu_bash() {
  while true; do
    echo; hr; echo "llmproxy tests  —  BASE=$BASE"; hr
    list_tests
    echo " q) esci"
    printf "Scelta: "
    local c; read -r c
    case "$c" in
      q|Q|"") break ;;
      *) if [[ "$c" =~ ^[0-9]+$ ]]; then echo; run_test "$c"; else echo "Scelta non valida"; fi ;;
    esac
  done
}

menu() {
  if [ "$USE_TUI" = 1 ] && { have whiptail || have dialog; }; then
    menu_tui_whiptail
  else
    menu_bash
  fi
}

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

main() {
  local arg=""
  for a in "$@"; do
    case "$a" in
      --no-tui) USE_TUI=0 ;;
      --list)   list_tests; exit 0 ;;
      -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
      *) arg="$a" ;;
    esac
  done

  if [ -n "$arg" ]; then
    run_test "$arg"
  else
    menu
  fi
}

main "$@"
