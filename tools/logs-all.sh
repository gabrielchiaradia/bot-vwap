#!/bin/bash
# ============================================================================
#  logs-all.sh — Ver logs de los bots VWAP con colores
# ============================================================================

# Colores
CYAN='\033[0;36m'
GREEN='\033[0;32m'
RESET='\033[0m'
BOLD='\033[1m'
DIM='\033[2m'

# Containers y sus prefijos/colores (Nombres exactos del docker-compose)
declare -A BOT_COLOR BOT_PREFIX
BOT_PREFIX[vwap-eth]="VWAP-ETH"
BOT_PREFIX[vwap-btc]="VWAP-BTC"

BOT_COLOR[vwap-eth]="$CYAN"
BOT_COLOR[vwap-btc]="$GREEN"

FOLLOW=true
TAIL=20

echo -e "${BOLD}VWAP BOTS — LOG VIEWER${RESET}"
echo -e "${DIM}Containers: vwap-eth, vwap-btc${RESET}"
echo ""

ACTIVE=()
for c in vwap-eth vwap-btc; do
    status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo "not_found")
    if [[ "$status" == "running" ]]; then
        ACTIVE+=("$c")
        echo -e "  ${BOT_COLOR[$c]}●${RESET} ${BOLD}${BOT_PREFIX[$c]}${RESET} ${DIM}— running${RESET}"
    else
        echo -e "  ${DIM}○${RESET} ${BOT_PREFIX[$c]} — ${status}"
    fi
done

if [[ ${#ACTIVE[@]} -eq 0 ]]; then
    echo -e "\nNo hay contenedores activos."
    exit 1
fi

cleanup() {
    pkill -P $$ 2>/dev/null || true
    echo -e "\n${DIM}Logs detenidos.${RESET}"
}
trap cleanup EXIT INT TERM

echo ""
for c in "${ACTIVE[@]}"; do
    color="${BOT_COLOR[$c]}"
    prefix="${BOT_PREFIX[$c]}"
    # Lanzar logs en paralelo con prefijo de color
    docker logs -f --tail "$TAIL" "$c" 2>&1 | sed "s/^/${color}[${prefix}]${RESET} /" &
done

wait