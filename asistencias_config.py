# asistencias_config.py

# Minutos para considerar “rebote” (doble toque inmediato)
WINDOW_REBOTE_MIN = 2

# Si la PRIMERA marca del día es una SALIDA antes de esta hora → se reclasifica a ENTRADA
HORA_UMBRAL_SALIDA_INICIAL = "10:00"   # HH:MM

# Autocierre
AUTO_CLOSE_UMBRAL_HORAS = 12 # si pasan ≥ 16h con entrada abierta
AUTO_CLOSE_MODO = "entrada_mas_10h"   # "entrada_mas_10h" | "fin_jornada"
HORA_FIN_JORNADA = "18:00"            # usado si AUTO_CLOSE_MODO == "fin_jornada"
