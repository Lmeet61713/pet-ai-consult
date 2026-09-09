#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file="${project_root}/.env.docker"
compose_file="${project_root}/compose.yaml"
project_name="pet-consult"

require_env() {
  if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file. Run '$0 init' first." >&2
    exit 1
  fi
  if grep -Eq '^(APP_SECRET_KEY|JWT_SIGNING_SECRET|LOG_HASH_SECRET|REDIS_PASSWORD)=change-me$' "$env_file"; then
    echo "Replace placeholder secrets in $env_file before starting." >&2
    exit 1
  fi
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
  if [[ ! -d "$CONSULT_VISION_MODEL_PATH" ]]; then
    echo "Model directory does not exist: $CONSULT_VISION_MODEL_PATH" >&2
    exit 1
  fi
  if find "$CONSULT_VISION_MODEL_PATH" -maxdepth 1 -name '*.incomplete' -print -quit | grep -q .; then
    echo "Model download is incomplete: $CONSULT_VISION_MODEL_PATH" >&2
    exit 1
  fi
}

compose() {
  docker compose --project-name "$project_name" --env-file "$env_file" \
    --file "$compose_file" "$@"
}

case "${1:-}" in
  init)
    if [[ -e "$env_file" ]]; then
      echo "$env_file already exists"
    else
      cp "${project_root}/.env.docker.example" "$env_file"
      chmod 0600 "$env_file"
      echo "Created $env_file; set secrets, model path, GPU, and API credentials."
    fi
    ;;
  up)
    require_env
    compose up -d --build --remove-orphans
    compose ps
    ;;
  down)
    require_env
    compose down
    ;;
  restart)
    require_env
    compose up -d --build --force-recreate --remove-orphans
    compose ps
    ;;
  status)
    require_env
    compose ps
    ;;
  logs)
    require_env
    compose logs --tail 200 -f "${@:2}"
    ;;
  config)
    require_env
    compose config
    ;;
  *)
    echo "Usage: $0 {init|up|down|restart|status|logs|config}" >&2
    exit 2
    ;;
esac
