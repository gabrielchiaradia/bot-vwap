# 📈 Scalping VWAP Reversion Bot v3

Bot de trading algorítmico para Binance Futures basado en **Reversión a la Media Institucional**. Utiliza el VWAP y bandas de desviación estándar para capturar rebotes de alta probabilidad.

## 🚀 Arquitectura Pro
- **Multi-Instancia:** Corriendo en contenedores Docker independientes para BTC y ETH.
- **Entradas Maker:** Utiliza órdenes LIMIT para maximizar el Profit Factor y reducir comisiones.
- **Dashboard Live:** Monitoreo en tiempo real mediante Nginx y JSON dinámicos.
- **Notificaciones Async:** Alertas de Telegram que no bloquean la ejecución del bot.

## 📊 Estrategia (Backtest 1Y)
- **Winrate:** ~87%
- **Riesgo por Trade:** 4% (configurable)
- **RR Target:** 0.4 (Optimizado para alta probabilidad)

## 🛠️ Despliegue con Docker
1. Clonar el repositorio.
2. Configurar `.env.eth` y `.env.btc` con tus API Keys.
3. Ejecutar:
   ```bash
   docker-compose up -d