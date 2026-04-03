"""
bot/websocket_stream.py
────────────────────────
Stream de velas en tiempo real via Binance WebSocket.

Reemplaza el polling periódico (sleep + REST) por un stream continuo
que reacciona exactamente al cierre de cada vela sin desperdiciar
llamadas API ni introducir delay.

Flujo:
  BinanceKlineStream → callback on_candle_close() → analizar → decidir

Ventajas sobre polling:
  - Reacciona al ms en que cierra la vela (no hasta el próximo sleep)
  - 0 llamadas REST innecesarias
  - Reconexión automática con backoff exponencial
  - Estado de velas acumulado en memoria (deque circular)
"""

from __future__ import annotations

import json
import time
import threading
import websocket          # websocket-client
import pandas as pd
import numpy as np
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Optional
from src.logger import logger


# ══════════════════════════════════════════════════════════
#  BUFFER DE VELAS
# ══════════════════════════════════════════════════════════

class CandleBuffer:
    """
    Buffer circular de velas OHLCV en memoria.
    Se actualiza con cada mensaje WebSocket.
    Cuando se pide el DataFrame, incluye la vela actual (no cerrada)
    y las N anteriores cerradas.
    """

    def __init__(self, maxlen: int = 300):
        self._closed:  deque = deque(maxlen=maxlen)   # Velas cerradas
        self._current: Optional[dict] = None           # Vela en construcción
        self._lock = threading.Lock()

    def update(self, kline: dict):
        """Procesa un mensaje kline del WebSocket."""
        with self._lock:
            candle = {
                "timestamp": pd.Timestamp(kline["t"], unit="ms", tz="UTC"),
                "open":      float(kline["o"]),
                "high":      float(kline["h"]),
                "low":       float(kline["l"]),
                "close":     float(kline["c"]),
                "volume":    float(kline["v"]),
                "closed":    kline["x"],   # True = vela cerrada
            }
            if candle["closed"]:
                self._closed.append(candle)
                self._current = None
                return True   # Señal de nueva vela cerrada
            else:
                self._current = candle
                return False

    def get_dataframe(self, n: int = 200) -> pd.DataFrame:
        """
        Retorna DataFrame con las últimas N velas cerradas.
        NO incluye la vela actual (para evitar lookahead).
        """
        with self._lock:
            velas = list(self._closed)[-n:]
            if not velas:
                return pd.DataFrame()
            df = pd.DataFrame(velas)
            df.set_index("timestamp", inplace=True)
            df.drop(columns=["closed"], inplace=True, errors="ignore")
            return df

    def get_precio_actual(self) -> float:
        """Retorna el precio de la vela en construcción."""
        with self._lock:
            if self._current:
                return self._current["close"]
            if self._closed:
                return self._closed[-1]["close"]
            return 0.0

    def tamanio(self) -> int:
        with self._lock:
            return len(self._closed)

    def seed(self, df_historico: pd.DataFrame):
        """
        Pre-carga velas históricas (REST) para tener contexto
        antes de que lleguen suficientes velas por WebSocket.
        """
        with self._lock:
            for ts, row in df_historico.iterrows():
                self._closed.append({
                    "timestamp": ts,
                    "open":   row["open"],
                    "high":   row["high"],
                    "low":    row["low"],
                    "close":  row["close"],
                    "volume": row["volume"],
                    "closed": True,
                })
        logger.info("Buffer pre-cargado con %d velas históricas", len(self._closed))


# ══════════════════════════════════════════════════════════
#  STREAM PRINCIPAL
# ══════════════════════════════════════════════════════════

class BinanceKlineStream:
    """
    Conecta al WebSocket de Binance y llama al callback
    `on_candle_close(df, buffer)` cada vez que cierra una vela.

    Maneja reconexión automática con backoff exponencial.
    """

    WS_URL = "wss://fstream.binance.com/ws/{symbol}@kline_{interval}"
    # Testnet usa diferente endpoint
    WS_URL_TEST = "wss://stream.binancefuture.com/ws/{symbol}@kline_{interval}"

    def __init__(
        self,
        symbol:           str,
        interval:         str,
        on_candle_close:  Callable[[pd.DataFrame, CandleBuffer], None],
        testnet:          bool = True,
        buffer_size:      int  = 300,
        candles_minimos:  int  = 50,   # Esperar N velas antes de analizar
    ):
        self.symbol          = symbol.lower()
        self.interval        = interval
        self.on_candle_close = on_candle_close
        self.testnet         = testnet
        self.buffer          = CandleBuffer(maxlen=buffer_size)
        self.candles_minimos = candles_minimos

        self._ws:          Optional[websocket.WebSocketApp] = None
        self._thread:      Optional[threading.Thread] = None
        self._running      = False
        self._reconexiones = 0
        self._ultimo_ping  = time.time()

    # ── API pública ────────────────────────────────────────

    def iniciar(self, df_historico: Optional[pd.DataFrame] = None):
        """
        Arranca el stream en un thread separado.
        Acepta DataFrame histórico para pre-cargar el buffer.
        """
        if df_historico is not None and not df_historico.empty:
            self.buffer.seed(df_historico)

        self._running = True
        self._thread  = threading.Thread(target=self._loop_reconexion,
                                          daemon=True, name="ws-stream")
        self._thread.start()
        logger.info("WebSocket stream iniciado: %s @ %s (testnet=%s)",
                    self.symbol.upper(), self.interval, self.testnet)

    def detener(self):
        """Para el stream limpiamente."""
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WebSocket stream detenido.")

    def precio_actual(self) -> float:
        return self.buffer.get_precio_actual()

    def listo(self) -> bool:
        """True cuando el buffer tiene suficientes velas para analizar."""
        return self.buffer.tamanio() >= self.candles_minimos

    # ── Loop de reconexión ─────────────────────────────────

    def _loop_reconexion(self):
        while self._running:
            try:
                self._conectar()
            except Exception as e:
                logger.error("Error inesperado en WebSocket: %s", e)

            if not self._running:
                break

            self._reconexiones += 1
            if self._reconexiones > 5:
                logger.error("Máximo de reconexiones alcanzado (%d). Deteniendo stream.", 5)
                self._running = False
                break

            # Backoff exponencial: 5s, 10s, 20s, 40s... máx 120s
            espera = min(5 * (2 ** (self._reconexiones - 1)), 120)
            logger.warning("WebSocket desconectado. Reconectando en %ds (intento %d)...",
                           espera, self._reconexiones)
            time.sleep(espera)

    def _conectar(self):
        url_template = self.WS_URL_TEST if self.testnet else self.WS_URL
        url = url_template.format(symbol=self.symbol, interval=self.interval)
        logger.debug("Conectando a: %s", url)

        self._ws = websocket.WebSocketApp(
            url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        # run_forever bloquea hasta que se cierra la conexión
        self._ws.run_forever(
            ping_interval = 20,
            ping_timeout  = 10,
        )

    # ── Callbacks WebSocket ────────────────────────────────

    def _on_open(self, ws):
        self._reconexiones = 0
        logger.info("WebSocket conectado: %s @ %s", self.symbol.upper(), self.interval)

    def _on_message(self, ws, message: str):
        try:
            data  = json.loads(message)
            kline = data.get("k", {})
            if not kline:
                return

            nueva_vela_cerrada = self.buffer.update(kline)

            if nueva_vela_cerrada:
                logger.debug("Vela cerrada | Close: $%.2f | Buffer: %d velas",
                             float(kline["c"]), self.buffer.tamanio())

                if not self.listo():
                    logger.debug("Buffer aún cargando (%d/%d velas)...",
                                 self.buffer.tamanio(), self.candles_minimos)
                    return

                # Llamar al callback con el DataFrame actualizado
                df = self.buffer.get_dataframe(n=1500)
                if not df.empty:
                    try:
                        self.on_candle_close(df, self.buffer)
                    except Exception as e:
                        logger.error("Error en on_candle_close: %s", e, exc_info=True)

        except json.JSONDecodeError:
            logger.warning("Mensaje WebSocket no es JSON válido")
        except Exception as e:
            logger.error("Error procesando mensaje WS: %s", e)

    def _on_error(self, ws, error):
        logger.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        logger.warning("WebSocket cerrado: code=%s msg=%s",
                       close_status_code, close_msg)


# ══════════════════════════════════════════════════════════
#  STREAM DE MARK PRICE (intra-vela)
# ══════════════════════════════════════════════════════════

class MarkPriceStream:
    """
    Stream de mark price en tiempo real (~1s de frecuencia).
    Llama a on_tick(mark_price) cada vez que llega un precio nuevo.
    Se usa para detectar toques de banda intra-vela sin esperar el cierre.
    """

    WS_URL      = "wss://fstream.binance.com/ws/{symbol}@markPrice"
    WS_URL_TEST = "wss://stream.binancefuture.com/ws/{symbol}@markPrice"

    def __init__(
        self,
        symbol:   str,
        on_tick:  Callable[[float], None],
        testnet:  bool = True,
    ):
        self.symbol   = symbol.lower()
        self.on_tick  = on_tick
        self.testnet  = testnet

        self._ws:      Optional[websocket.WebSocketApp] = None
        self._thread:  Optional[threading.Thread] = None
        self._running  = False
        self._reconexiones = 0

    def iniciar(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop_reconexion,
                                          daemon=True, name="markprice-stream")
        self._thread.start()
        logger.info("MarkPrice stream iniciado: %s (testnet=%s)",
                    self.symbol.upper(), self.testnet)

    def detener(self):
        self._running = False
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("MarkPrice stream detenido.")

    def _loop_reconexion(self):
        while self._running:
            try:
                self._conectar()
            except Exception as e:
                logger.error("Error en MarkPrice stream: %s", e)

            if not self._running:
                break

            self._reconexiones += 1
            if self._reconexiones > 10:
                logger.error("MarkPrice: máximo reconexiones. Deteniendo.")
                self._running = False
                break

            espera = min(5 * (2 ** (self._reconexiones - 1)), 60)
            logger.warning("MarkPrice desconectado. Reconectando en %ds...", espera)
            time.sleep(espera)

    def _conectar(self):
        url_template = self.WS_URL_TEST if self.testnet else self.WS_URL
        url = url_template.format(symbol=self.symbol)
        logger.debug("MarkPrice conectando a: %s", url)

        self._ws = websocket.WebSocketApp(
            url,
            on_open    = self._on_open,
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        self._reconexiones = 0
        logger.info("MarkPrice conectado: %s", self.symbol.upper())

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
            mark = float(data.get("p", 0))
            if mark > 0:
                self.on_tick(mark)
        except Exception as e:
            logger.error("Error en MarkPrice message: %s", e)

    def _on_error(self, ws, error):
        logger.error("MarkPrice error: %s", error)

    def _on_close(self, ws, code, msg):
        logger.warning("MarkPrice cerrado: code=%s", code)
