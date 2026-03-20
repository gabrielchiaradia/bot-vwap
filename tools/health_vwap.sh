#!/bin/bash
# ============================================================================
#  health-vwap.sh — Monitoreo Exclusivo para Bots VWAP (BTC/ETH)
#  Uso:
#    sudo ./health-vwap.sh --install     # Instala en crontab
#    sudo ./health-vwap.sh --uninstall   # Elimina del crontab
#    ./health-vwap.sh                    # Ejecución manual
# ============================================================================

# --- CONFIGURACIÓN ---
BOT_USER="botuser"  # Cambialo por tu usuario de Linux si es distinto
BOT_DIR="/home/$BOT_USER/bot-vwap"
HC_LOG="$BOT_DIR/logs/health_vwap.log"
SCRIPT_NAME="health-vwap.sh"

# Intentar leer Telegram del .env.eth para las alertas
if [[ -f "$BOT_DIR/.env.eth" ]]; then
    TELEGRAM_BOT_TOKEN=$(grep 'TELEGRAM_BOT_TOKEN' "$BOT_DIR/.env.eth" | cut -d'=' -f2)
    TELEGRAM_CHAT_ID=$(grep 'TELEGRAM_CHAT_ID' "$BOT_DIR/.env.eth" | cut -d'=' -f2)
fi

# --- FUNCIONES ---
send_telegram() {
    local msg="⚠️ <b>VWAP HEALTH</b>\n$1"
    if [[ ! -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
            -d "chat_id=$TELEGRAM_CHAT_ID" -d "text=$msg" -d "parse_mode=HTML" > /dev/null
    fi
}

run_check() {
    local name=$1
    echo -n "[$(date '+%Y-%m-%d %H:%M:%S')] Checking $name... "
    
    # Verifica si el contenedor existe y está corriendo
    status=$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo "false")
    
    if [[ "$status" != "true" ]]; then
        echo "❌ CAÍDO. Reiniciando..."
        send_telegram "El bot <b>$name</b> está fuera de línea. Reiniciando contenedor..."
        docker start "$name"
    else
        # Verifica si hubo actividad de logs en los últimos 5 minutos
        # Si el bot se tilda (loop infinito o error de API), no genera logs
        last_log=$(docker logs --since 5m "$name" 2>&1 | wc -l)
        if [[ $last_log -eq 0 ]]; then
            echo "⚠️ CONGELADO."
            send_telegram "El bot <b>$name</b> está corriendo pero no genera actividad (5m sin logs)."
        else
            echo "✅ OPERATIVO"
        fi
    fi
}

install_cron() {
    if [[ $EUID -ne 0 ]]; then
       echo "Error: --install debe ejecutarse como ROOT (sudo)."
       exit 1
    fi
    
    mkdir -p "$(dirname "$HC_LOG")"
    touch "$HC_LOG"
    chown "$BOT_USER:$BOT_USER" "$HC_LOG"
    chmod +x "$0"

    # CRON específico: Se diferencia por el nombre del script para no borrar otros checks
    CRON_LINE="*/5 * * * * cd $BOT_DIR && sudo -u $BOT_USER ./$SCRIPT_NAME >> $HC_LOG 2>&1"
    
    # Agregamos al crontab sin borrar lo que ya existe de otros bots
    (crontab -l 2>/dev/null | grep -v "$SCRIPT_NAME" ; echo "$CRON_LINE") | crontab -
    
    echo "✅ health-vwap instalado."
    echo "Frecuencia: Cada 5 min"
    echo "Usuario: $BOT_USER"
}

uninstall_cron() {
    if [[ $EUID -ne 0 ]]; then
       echo "Error: --uninstall debe ejecutarse como ROOT (sudo)."
       exit 1
    fi
    # Elimina SOLO la línea que contiene este script
    (crontab -l 2>/dev/null | grep -v "$SCRIPT_NAME") | crontab -
    echo "✅ health-vwap desinstalado (Cronjob eliminado)."
}

# --- MAIN ---
case "${1:-}" in
    --install)
        install_cron
        ;;
    --uninstall)
        uninstall_cron
        ;;
    *)
        # Ejecuta el chequeo para tus 2 contenedores VWAP
        run_check "vwap-eth"
        run_check "vwap-btc"
        ;;
esac