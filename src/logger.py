import logging
import os
from datetime import datetime

# Crear carpeta de logs si no existe
if not os.path.exists('logs'):
    os.makedirs('logs')

# Configuración del nombre del archivo (un archivo nuevo por día)
log_filename = f"logs/bot_{datetime.now().strftime('%Y-%m-%d')}.log"

# Formato de los mensajes
log_format = logging.Formatter(
    '%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 1. Configurar el Logger Principal
logger = logging.getLogger("VWAP_Bot")
logger.setLevel(logging.DEBUG) # Capturamos todo desde DEBUG para arriba

# 2. Handler para Archivo (Guarda todo, incluyendo errores detallados)
file_handler = logging.FileHandler(log_filename, encoding='utf-8')
file_handler.setFormatter(log_format)
file_handler.setLevel(logging.INFO)

# 3. Handler para Consola (Stream) con colores simples para legibilidad
class ColorFormatter(logging.Formatter):
    """Añade colores a los niveles de log en la consola"""
    grey = "\x1b[38;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    blue = "\x1b[36;20m"
    reset = "\x1b[0m"
    format_str = "%(asctime)s | %(levelname)-8s | %(message)s"

    FORMATS = {
        logging.DEBUG: blue + format_str + reset,
        logging.INFO: grey + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%H:%M:%S')
        return formatter.format(record)

console_handler = logging.StreamHandler()
console_handler.setFormatter(ColorFormatter())
console_handler.setLevel(logging.INFO)

# Agregar handlers al logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Evitar que los mensajes se dupliquen si se importa en varios archivos
logger.propagate = False