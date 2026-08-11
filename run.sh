#!/usr/bin/env bash
# Anonimizador · beailabs — arranque local.
# Crea el entorno virtual, instala dependencias y levanta el servidor.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"
MODEL="${ANON_MODEL:-qwen3:4b}"
OLLAMA="http://localhost:11434"

if [ ! -d ".venv" ]; then
  echo "› Creando entorno virtual…"
  "$PYTHON" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "› Instalando dependencias…"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

# Comprobar Ollama y, si está corriendo pero falta el modelo, descargarlo solo.
if ! curl -s "${OLLAMA}/api/tags" -m 3 >/dev/null 2>&1; then
  echo ""
  echo "⚠️  Ollama no responde en ${OLLAMA} (la app cargará, pero no anonimizará)."
  echo "   1) Instálalo:      brew install ollama   (o https://ollama.com/download)"
  echo "   2) Arráncalo:      ollama serve          (o abre la app de Ollama)"
  echo "   3) Vuelve a lanzar ./run.sh y el modelo se descargará solo."
  echo ""
elif curl -s "${OLLAMA}/api/tags" -m 5 | grep -q "\"${MODEL}\""; then
  echo "› Modelo ${MODEL} ya disponible."
else
  echo "› Modelo ${MODEL} no encontrado. Descargándolo (~2.5 GB la primera vez)…"
  # La API /api/pull emite líneas JSON con el estado; mostramos la última.
  curl -s -N -X POST "${OLLAMA}/api/pull" -d "{\"name\":\"${MODEL}\"}" \
    | while IFS= read -r line; do
        st=$(printf '%s' "$line" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
        [ -n "$st" ] && printf '\r  %-55s' "$st"
        true
      done || true
  printf '\n'
  if curl -s "${OLLAMA}/api/tags" -m 5 | grep -q "\"${MODEL}\""; then
    echo "› Modelo ${MODEL} listo."
  else
    echo "⚠️  No se pudo descargar ${MODEL}. Descárgalo a mano: ollama pull ${MODEL}"
  fi
fi

echo "› Servidor en http://localhost:${PORT}"
( sleep 1.5; (open "http://localhost:${PORT}" 2>/dev/null || true) ) &

cd backend
exec python -m uvicorn app:app --host 127.0.0.1 --port "${PORT}"
