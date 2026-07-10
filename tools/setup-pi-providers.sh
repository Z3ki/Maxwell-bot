#!/usr/bin/env bash
# setup-pi-providers.sh
# Maps Maxwell .env providers to Pi's expected env / auth.json for "same providers".
# Run inside the Pi Debian Docker or host before `pi`.
# Safe: only reads .env or current env; writes ~/.pi/agent/auth.json (0600).

set -euo pipefail

ENV_FILE="${MAXWELL_ENV_FILE:-.env}"
PI_AUTH_DIR="${HOME}/.pi/agent"
PI_AUTH_FILE="${PI_AUTH_DIR}/auth.json"

mkdir -p "$PI_AUTH_DIR"
chmod 700 "$PI_AUTH_DIR" 2>/dev/null || true

# Load .env if present (simple, no override of real env)
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^[A-Z_]+=' "$ENV_FILE" | sed 's/export //') || true
  set +a
fi

declare -A MAP=(
  ["openrouter"]="OPENROUTER_API_KEY"
  ["nvidia"]="NVIDIA_API_KEY"
  ["anthropic"]="ANTHROPIC_API_KEY"
  ["openai"]="OPENAI_API_KEY"
  ["xai"]="XAI_API_KEY"
  ["groq"]="GROQ_API_KEY"
  ["gemini"]="GEMINI_API_KEY"
  ["mistral"]="MISTRAL_API_KEY"
  ["together"]="TOGETHER_API_KEY"
  ["fireworks"]="FIREWORKS_API_KEY"
  ["huggingface"]="HF_TOKEN"
  # Add more as needed. Maxwell primary keys often live under OLLAMA_* or AUTONOMY_*
)

# Fallback mappings from Maxwell naming
if [[ -z "${OPENROUTER_API_KEY:-}" && -n "${OLLAMA_FALLBACK_API_KEY:-}" ]]; then
  OPENROUTER_API_KEY="$OLLAMA_FALLBACK_API_KEY"
fi
if [[ -z "${NVIDIA_API_KEY:-}" && -n "${AUTONOMY_API_KEY:-}" ]]; then
  NVIDIA_API_KEY="$AUTONOMY_API_KEY"
fi

auth_json="{"
first=true
for pi_key in "${!MAP[@]}"; do
  env_var="${MAP[$pi_key]}"
  val="${!env_var:-}"
  if [[ -n "$val" ]]; then
    if $first; then first=false; else auth_json+=","; fi
    auth_json+="\"$pi_key\":{\"type\":\"api_key\",\"key\":\"$val\"}"
  fi
done
auth_json+="}"

if [[ "$auth_json" != "{}" ]]; then
  echo "$auth_json" > "$PI_AUTH_FILE"
  chmod 600 "$PI_AUTH_FILE"
  echo "Wrote Pi auth.json with available keys at $PI_AUTH_FILE"
else
  echo "No matching API keys found in env or $ENV_FILE. Pi will use /login or direct env vars."
fi

echo "Provider setup complete. Run 'pi' or 'pi --list-models' to verify."
echo "Common Maxwell->Pi: OPENROUTER_API_KEY, NVIDIA_API_KEY (autonomy/img), etc."
