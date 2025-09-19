# asistencias_rules.py
from datetime import datetime, timedelta
from asistencias_config import (
    WINDOW_REBOTE_MIN, HORA_UMBRAL_SALIDA_INICIAL,
    AUTO_CLOSE_UMBRAL_HORAS, AUTO_CLOSE_MODO, HORA_FIN_JORNADA
)

FMT = "%Y-%m-%dT%H:%M:%S%z"  # ISO con zona si ya viene así; ajusta si usas naive

def _to_dt(x):
    # Acepta datetime o string ISO. Ajusta si tus timestamps son epoch.
    if isinstance(x, datetime):
        return x
    return datetime.fromisoformat(x)

def _time_str_to_today(fecha, hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return fecha.replace(hour=hh, minute=mm, second=0, microsecond=0)

def _minutes_diff(a, b):
    return abs(int((_to_dt(a) - _to_dt(b)).total_seconds() // 60))

def _dedupe_rebotes(eventos):
    """Elimina rebotes (mismo tipo pegado). Marca duplicados."""
    out = []
    last_of_type = {}  # tipo -> dt última
    for e in eventos:
        t = e.get("tipo")
        dt = _to_dt(e["fechaHora"])
        if t in ("Entrada", "Salida"):
            last_dt = last_of_type.get(t)
            if last_dt and _minutes_diff(dt, last_dt) <= WINDOW_REBOTE_MIN:
                e["duplicado"] = True
                out.append(e)  # lo guardamos para auditoría, pero no contará
                continue
            last_of_type[t] = dt
        out.append(e)
    return out

def _clasificar_desconocidos(eventos):
    """Marca como ignorados los tipos distintos a Entrada/Salida."""
    for e in eventos:
        if e.get("tipo") not in ("Entrada", "Salida"):
            e["ignorado"] = True
    return eventos

def _reclasificar_salida_inicial_si_corresponde(eventos):
    """Si la PRIMERA marca es SALIDA antes de umbral → reclasificar a ENTRADA."""
    if not eventos:
        return eventos
    primero = eventos[0]
    if primero.get("tipo") == "Salida":
        dt0 = _to_dt(primero["fechaHora"])
        umbral = _time_str_to_today(dt0, HORA_UMBRAL_SALIDA_INICIAL)
        if dt0 <= umbral:
            primero["reclasificado"] = True
            primero["correccion"] = "salida_inicial_reclasificada"
            primero["tipo"] = "Entrada"
    return eventos

def _pairing(eventos):
    """
    Alternancia Entrada -> Salida.
    Marca extra_entrada / extra_salida y produce pares válidos.
    Devuelve (eventos_saneados, hay_autocierre, hay_par_valido, entrada_abierta_dt)
    """
    abiertos = None  # dt de entrada abierta
    par_valido = False
    for e in eventos:
        if e.get("ignorado") or e.get("duplicado"):
            continue

        t = e.get("tipo")
        if t == "Entrada":
            if abiertos is None:
                abiertos = _to_dt(e["fechaHora"])
            else:
                # doble entrada con una ya abierta
                e["extra_entrada"] = True
        elif t == "Salida":
            if abiertos is None:
                # salida suelta
                e["extra_salida"] = True
            else:
                # cerrar ciclo
                par_valido = True
                abiertos = None
        # otros ya están ignorados
    return eventos, par_valido, abiertos

def _autocierre_si_necesario(eventos, entrada_abierta_dt):
    if entrada_abierta_dt is None:
        return eventos, False
    # ¿Aplica autocierre?
    ahora = max(_to_dt(e["fechaHora"]) for e in eventos)
    if (ahora - entrada_abierta_dt) >= timedelta(hours=AUTO_CLOSE_UMBRAL_HORAS):
        # Generar salida sintética
        if AUTO_CLOSE_MODO == "fin_jornada":
            salida_dt = _time_str_to_today(entrada_abierta_dt, HORA_FIN_JORNADA)
            if salida_dt < entrada_abierta_dt:
                salida_dt = entrada_abierta_dt + timedelta(hours=8)
        else:
            salida_dt = entrada_abierta_dt + timedelta(hours=10)
        eventos.append({
            "tipo": "Salida",
            "fechaHora": salida_dt.isoformat(),
            "origen": "auto",
            "salida_automatica": True,
            "correccion": "auto_close"
        })
        return eventos, True
    return eventos, False

def sanear_eventos_dia(eventos):
    """
    eventos: lista de dicts con al menos:
      { "tipo": "Entrada"|"Salida"|otro, "fechaHora": datetime|iso }
    Devuelve: (eventos_limpios, estado)
    """
    # 1) ordenar por fecha
    eventos = sorted(eventos, key=lambda e: _to_dt(e["fechaHora"]))

    # 2) marcar desconocidos
    eventos = _clasificar_desconocidos(eventos)

    # 3) dedupe rebotes solo para Entrada/Salida
    eventos = _dedupe_rebotes(eventos)

    # 4) regla especial: primera salida del día -> entrada (si aplica)
    eventos = _reclasificar_salida_inicial_si_corresponde(eventos)

    # 5) pairing + flags
    eventos, hay_par_valido, entrada_abierta_dt = _pairing(eventos)

    # 6) autocierre si corresponde
    eventos, hizo_auto = _autocierre_si_necesario(eventos, entrada_abierta_dt)

    # 7) estado final
    if hizo_auto:
        estado = "Asistencia con salida automática"
    elif hay_par_valido:
        # si quedó entrada abierta pero aún no pasa el umbral → Pendiente
        if entrada_abierta_dt is not None:
            estado = "Pendiente"
        else:
            estado = "Asistencia Completa"
    else:
        # sin pares válidos
        if any(e.get("tipo") == "Entrada" and not e.get("duplicado") and not e.get("ignorado") for e in eventos):
            estado = "Pendiente"
        else:
            estado = "Sin datos válidos"

    return eventos, estado
