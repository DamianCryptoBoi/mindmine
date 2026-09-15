#!/usr/bin/env bash

set -euo pipefail

readonly PROJECT_DIRECTORY="$(pwd -P)"
readonly SOURCE_ENV="$PROJECT_DIRECTORY/.env"
readonly TEMPLATE_FILE="$PROJECT_DIRECTORY/.env.gen_miner.hotkey.template"
readonly PM2_CONFIG="$PROJECT_DIRECTORY/gen_miner.config.js"

info() {
    printf '[miner-setup] %s\n' "$1"
}

fail() {
    printf '[miner-setup] ERROR: %s\n' "$1" >&2
    exit 1
}

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

read_env_rhs() {
    local wanted="$1"
    local line key found=""

    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="$(trim "$line")"
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" == export[[:space:]]* ]]; then
            line="$(trim "${line#export}")"
        fi
        [[ "$line" == *=* ]] || continue

        key="$(trim "${line%%=*}")"
        [[ "$key" == "$wanted" ]] || continue
        found="${line#*=}"
    done < "$SOURCE_ENV"

    printf '%s' "$found"
}

read_env_value() {
    local value
    value="$(trim "$(read_env_rhs "$1")")"
    if [[ "${value:0:1}" == '"' ]]; then
        value="${value:1}"
        value="${value%%\"*}"
    elif [[ "${value:0:1}" == "'" ]]; then
        value="${value:1}"
        value="${value%%\'*}"
    elif [[ "$value" == \#* ]]; then
        value=""
    elif [[ "$value" =~ ^(.*[^[:space:]])[[:space:]]+\#.*$ ]]; then
        value="${BASH_REMATCH[1]}"
    fi
    trim "$value"
}

service_label() {
    case "$1" in
        openai) printf 'OpenAI' ;;
        openrouter) printf 'OpenRouter' ;;
        stabilityai) printf 'Stability AI' ;;
        runway) printf 'Runway' ;;
        maxcheapai) printf 'MaxCheapAI' ;;
        ckey) printf 'CKey' ;;
        vertexgen) printf 'VertexGen' ;;
        gpti2) printf 'GPTi2' ;;
        *) return 1 ;;
    esac
}

service_key_name() {
    case "$1" in
        openai) printf 'OPENAI_API_KEY' ;;
        openrouter) printf 'OPEN_ROUTER_API_KEY' ;;
        stabilityai) printf 'STABILITY_API_KEY' ;;
        runway)
            if [[ -n "$(read_env_value RUNWAYML_API_KEY)" ]]; then
                printf 'RUNWAYML_API_KEY'
            else
                printf 'RUNWAYML_API_SECRET'
            fi
            ;;
        maxcheapai) printf 'MAXCHEAPAI_API_KEY' ;;
        ckey) printf 'CKEY_API_KEY' ;;
        vertexgen) printf 'VERTEXGEN_API_KEY' ;;
        gpti2) printf 'GPTI2_API_KEY' ;;
        none) printf '' ;;
        *) return 1 ;;
    esac
}

has_service_key() {
    local key
    key="$(service_key_name "$1")"
    [[ -n "$key" && -n "$(read_env_value "$key")" ]]
}

choose_service() {
    local modality="$1"
    shift
    local options=("$@")
    local index choice

    if [[ ${#options[@]} -eq 0 ]]; then
        info "No keyed $modality service is available; using none." >&2
        printf 'none'
        return
    fi

    printf 'Available %s services:\n' "$modality" >&2
    for ((index = 0; index < ${#options[@]}; index++)); do
        printf '  %d) %s\n' "$((index + 1))" "$(service_label "${options[$index]}")" >&2
    done
    printf '  %d) None\n' "$(( ${#options[@]} + 1 ))" >&2

    while true; do
        printf 'Select %s service: ' "$modality" >&2
        IFS= read -r choice || fail "No $modality service selection was provided."
        if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#options[@]})); then
            printf '%s' "${options[$((choice - 1))]}"
            return
        fi
        if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice == ${#options[@]} + 1)); then
            printf 'none'
            return
        fi
        printf 'Enter a number from 1 to %d.\n' "$(( ${#options[@]} + 1 ))" >&2
    done
}

is_provider_key() {
    case "$1" in
        OPENAI_API_KEY|OPEN_ROUTER_API_KEY|STABILITY_API_KEY|RUNWAYML_API_KEY|RUNWAYML_API_SECRET|MAXCHEAPAI_API_KEY|CKEY_API_KEY|VERTEXGEN_API_KEY|GPTI2_API_KEY)
            return 0
            ;;
        *) return 1 ;;
    esac
}

contains_key() {
    local wanted="$1"
    shift
    local candidate
    for candidate in "$@"; do
        [[ "$candidate" == "$wanted" ]] && return 0
    done
    return 1
}

find_unused_axon_port() {
    python3 - "$@" <<'PY'
import random
import socket
import sys

excluded = {int(port) for port in sys.argv[1:] if port}
ports = [port for port in range(8000, 9001) if port not in excluded]
random.SystemRandom().shuffle(ports)
for port in ports:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        continue
    finally:
        sock.close()
    print(port)
    break
else:
    raise SystemExit("no unused TCP port is available from 8000 through 9000")
PY
}

[[ -f "$SOURCE_ENV" ]] || fail "Missing .env in the repository root."
[[ -f "$TEMPLATE_FILE" ]] || fail "Missing .env.gen_miner.hotkey.template in the repository root."
[[ -f "$PM2_CONFIG" ]] || fail "Missing gen_miner.config.js in the repository root."
command -v curl >/dev/null 2>&1 || fail "curl is required."
command -v python3 >/dev/null 2>&1 || fail "python3 is required."
command -v pm2 >/dev/null 2>&1 || fail "pm2 is required."

printf 'Wallet name: ' >&2
IFS= read -r wallet_name || fail "No wallet name was provided."
printf 'Hotkeys (space-separated): ' >&2
read -r -a hotkeys || fail "No hotkeys were provided."

[[ "$wallet_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "Wallet name may contain only letters, numbers, dots, underscores, and hyphens."
[[ ${#hotkeys[@]} -gt 0 ]] || fail "No hotkeys were provided."
validated_hotkeys=()
for hotkey in "${hotkeys[@]}"; do
    [[ "$hotkey" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || fail "Hotkey may contain only letters, numbers, dots, underscores, and hyphens."
    ! contains_key "$hotkey" "${validated_hotkeys[@]-}" || fail "Duplicate hotkey: $hotkey."
    validated_hotkeys+=("$hotkey")
    env_file=".env.$hotkey"
    [[ ! -e "$PROJECT_DIRECTORY/$env_file" && ! -L "$PROJECT_DIRECTORY/$env_file" ]] || fail "$env_file already exists; refusing to overwrite it."
done

image_options=()
for service in openai openrouter stabilityai maxcheapai ckey vertexgen gpti2; do
    has_service_key "$service" && image_options+=("$service")
done

video_options=()
for service in openai openrouter runway maxcheapai; do
    has_service_key "$service" && video_options+=("$service")
done

if [[ ${#image_options[@]} -eq 0 && ${#video_options[@]} -eq 0 ]]; then
    fail "No image or video service API keys are configured in .env."
fi

image_service="$(choose_service image "${image_options[@]}")"
video_service="$(choose_service video "${video_options[@]}")"
if [[ "$image_service" == none && "$video_service" == none ]]; then
    fail "At least one image or video service must be enabled."
fi

external_ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com)" || fail "Could not detect the external IP address."
external_ip="$(trim "$external_ip")"
if ! python3 - "$external_ip" >/dev/null 2>&1 <<'PY'
import ipaddress
import sys

ipaddress.ip_address(sys.argv[1])
PY
then
    fail "The detected external IP address is invalid."
fi

image_key="$(service_key_name "$image_service")"
video_key="$(service_key_name "$video_service")"
replacement_keys=(
    MINER_PM2_NAME
    IMAGE_SERVICE
    VIDEO_SERVICE
    BT_WALLET_NAME
    BT_WALLET_HOTKEY
    BT_AXON_PORT
    BT_AXON_EXTERNAL_IP
    MINER_MAX_CONCURRENT_TASKS
    MINER_WORKER_THREADS
    MINER_TASK_TIMEOUT
    MINER_SAVE_LOCALLY
    MINER_OUTPUT_DIR
    MINER_STATE_DIR
    BT_LOGGING_LEVEL
    AUTO_UPDATE
)
[[ -n "$image_key" ]] && replacement_keys+=("$image_key")
if [[ -n "$video_key" ]] && ! contains_key "$video_key" "${replacement_keys[@]}"; then
    replacement_keys+=("$video_key")
fi

replacement_value() {
    case "$1" in
        MINER_PM2_NAME) printf '%s' "$hotkey" ;;
        IMAGE_SERVICE) printf '%s' "$image_service" ;;
        VIDEO_SERVICE) printf '%s' "$video_service" ;;
        BT_WALLET_NAME) printf '%s' "$wallet_name" ;;
        BT_WALLET_HOTKEY) printf '%s' "$hotkey" ;;
        BT_AXON_PORT) printf '%s' "$axon_port" ;;
        BT_AXON_EXTERNAL_IP) printf '%s' "$external_ip" ;;
        MINER_MAX_CONCURRENT_TASKS) printf '8' ;;
        MINER_WORKER_THREADS) printf '8' ;;
        MINER_TASK_TIMEOUT) printf '3600' ;;
        MINER_SAVE_LOCALLY) printf 'false' ;;
        MINER_OUTPUT_DIR) printf './miner_generated_content/' ;;
        MINER_STATE_DIR) printf './miner_generated_content/.gen_miner_state/%s' "$hotkey" ;;
        BT_LOGGING_LEVEL) printf 'INFO' ;;
        AUTO_UPDATE) printf 'false' ;;
        "$image_key"|"$video_key") read_env_rhs "$1" ;;
        *) return 1 ;;
    esac
}

umask 077
temp_path=""
cleanup() {
    if [[ -n "${temp_path:-}" && -e "$temp_path" ]]; then
        rm -f -- "$temp_path"
    fi
}
trap cleanup EXIT

axon_ports=()
for hotkey in "${hotkeys[@]}"; do
    axon_port="$(find_unused_axon_port "${axon_ports[@]-}")" || fail "Could not find an unused TCP port from 8000 through 9000."
    axon_ports+=("$axon_port")
    env_file=".env.$hotkey"
    target_path="$PROJECT_DIRECTORY/$env_file"
    temp_path="$(mktemp "$PROJECT_DIRECTORY/.env.$hotkey.tmp.XXXXXX")"

    written_keys=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]]; then
            key="${BASH_REMATCH[1]}"
            if contains_key "$key" "${replacement_keys[@]}"; then
                printf '%s=%s\n' "$key" "$(replacement_value "$key")" >> "$temp_path"
                written_keys+=("$key")
                continue
            fi
            if is_provider_key "$key"; then
                continue
            fi
        fi
        printf '%s\n' "$line" >> "$temp_path"
    done < "$TEMPLATE_FILE"

    for key in "${replacement_keys[@]}"; do
        if ! contains_key "$key" "${written_keys[@]}"; then
            printf '%s=%s\n' "$key" "$(replacement_value "$key")" >> "$temp_path"
        fi
    done

    chmod 600 "$temp_path"
    if ! ln "$temp_path" "$target_path" 2>/dev/null; then
        fail "$env_file already exists; refusing to overwrite it."
    fi
    rm -f -- "$temp_path"
    temp_path=""

    info "Created $env_file for wallet $wallet_name and hotkey $hotkey."
    info "Using external IP $external_ip and unused TCP port $axon_port."
    info "Starting PM2 process $hotkey with image=$image_service and video=$video_service."
    GEN_MINER_ENV_FILE="$env_file" pm2 start gen_miner.config.js
done
