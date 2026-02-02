# asistencias_rules.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any

from asistencias_config import (
    WINDOW_REBOTE_MIN,
    HORA_UMBRAL_SALIDA_INICIAL,
    AUTO_CLOSE_UMBRAL_HORAS,
    AUTO_CLOSE_MODO,
    HORA_FIN_JORNADA,
)

VALID_TIPOS = {"Entrada", "Salida"}


# ==========================================================
# Helpers robustos (para que no se rompa por formatos raros)
# ==========================================================
def _to_dt(x: Any) -> datetime:
    """
    Acepta:
      - datetime (naive o aware)
      - string ISO (soporta "Z")
    Devuelve datetime (tal cual lo parsea).
    """
    if isinstance(x, datetime):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    raise ValueError(f"fechaHora inválida ({type(x)}): {x}")


def _safe_get_dt(e: Dict) -> datetime | None:
    """Devuelve datetime o None si no se puede parsear."""
    try:
        return _to_dt(e.get("fechaHora"))
    except Exception:
        return None


def _time_str_to_same_day(base_dt: datetime, hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    return base_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _minutes_diff(a_dt: datetime, b_dt: datetime) -> int:
    return abs(int((a_dt - b_dt).total_seconds() // 60))


# ==========================================================
# Regla 0) Orden + filtrado mínimo de eventos inválidos
# ==========================================================
def _ordenar_y_marcar_invalidos(eventos: List[Dict]) -> List[Dict]:
    """
    - Ordena por fechaHora cuando se puede.
    - Si no se puede parsear fechaHora, marca 'invalido=True' y lo deja al final.
    """
    parsed = []
    invalid = []

    for e in eventos or []:
        dt = _safe_get_dt(e)
        if dt is None:
            e["invalido"] = True
            e["correccion"] = e.get("correccion") or "fechaHora_invalida"
            invalid.append(e)
        else:
            parsed.append((dt, e))

    parsed.sort(key=lambda pair: pair[0])
    out = [e for _, e in parsed] + invalid
    return out


# ==========================================================
# Regla 1) Clasificar desconocidos (tolerante, pero no rompe)
# ==========================================================
def _clasificar_desconocidos(eventos: List[Dict]) -> List[Dict]:
    """
    Marca ignorado=True si tipo no es Entrada/Salida.
    (En tu arquitectura ideal, esto ya viene corregido antes.)
    """
    for e in eventos:
        if e.get("invalido"):
            # si ni fecha tiene, lo ignoramos del cómputo
            e["ignorado"] = True
            continue

        t = e.get("tipo")
        if t not in VALID_TIPOS:
            e["ignorado"] = True
            # No lo cambiamos aquí; solo lo sacamos del cómputo
            e["correccion"] = e.get("correccion") or "tipo_no_valido_en_rules"
    return eventos


# ==========================================================
# Regla 2) Rebotes: mismas marcas muy seguidas (auditoría)
# ==========================================================
def _dedupe_rebotes(eventos: List[Dict]) -> List[Dict]:
    """
    Si hay dos marcas del mismo tipo (Entrada/Salida) con diferencia <= WINDOW_REBOTE_MIN,
    marca la segunda como duplicado=True (no cuenta en pairing).
    """
    last_of_type: Dict[str, datetime] = {}

    for e in eventos:
        if e.get("ignorado") or e.get("duplicado") or e.get("invalido"):
            continue

        t = e.get("tipo")
        if t not in VALID_TIPOS:
            continue

        dt = _safe_get_dt(e)
        if dt is None:
            e["invalido"] = True
            e["ignorado"] = True
            e["correccion"] = e.get("correccion") or "fechaHora_invalida"
            continue

        last_dt = last_of_type.get(t)
        if last_dt is not None and _minutes_diff(dt, last_dt) <= WINDOW_REBOTE_MIN:
            e["duplicado"] = True
            e["correccion"] = e.get("correccion") or "rebote"
            continue

        last_of_type[t] = dt

    return eventos


# ==========================================================
# Regla 3) Salida inicial -> Entrada si está antes del umbral
# ==========================================================
def _reclasificar_salida_inicial_si_corresponde(eventos: List[Dict]) -> List[Dict]:
    """
    Si el primer evento CONTABLE del día (no ignorado/duplicado/invalido) es "Salida"
    y ocurre antes de HORA_UMBRAL_SALIDA_INICIAL -> se convierte a "Entrada".
    """
    # Encontrar el primer evento válido/contable
    primero = None
    for e in eventos:
        if e.get("ignorado") or e.get("duplicado") or e.get("invalido"):
            continue
        if e.get("tipo") in VALID_TIPOS:
            primero = e
            break

    if not primero:
        return eventos

    if primero.get("tipo") == "Salida":
        dt0 = _safe_get_dt(primero)
        if dt0 is None:
            return eventos

        umbral = _time_str_to_same_day(dt0, HORA_UMBRAL_SALIDA_INICIAL)
        if dt0 <= umbral:
            primero["tipo_original"] = primero.get("tipo_original", "Salida")
            primero["tipo"] = "Entrada"
            primero["reclasificado"] = True
            primero["inferido"] = True
            primero["correccion"] = "salida_inicial_reclasificada"

    return eventos


# ==========================================================
# Regla 4) Pairing: alternancia Entrada -> Salida
# ==========================================================
def _pairing(eventos: List[Dict]) -> Tuple[List[Dict], bool, datetime | None]:
    """
    Recorre eventos contables y marca:
      - extra_entrada si hay Entrada cuando ya hay una entrada abierta
      - extra_salida si hay Salida cuando no hay entrada abierta
    Devuelve:
      (eventos, hay_par_valido, entrada_abierta_dt)
    """
    entrada_abierta_dt = None
    hay_par_valido = False

    for e in eventos:
        if e.get("ignorado") or e.get("duplicado") or e.get("invalido"):
            continue

        t = e.get("tipo")
        if t not in VALID_TIPOS:
            continue

        dt = _safe_get_dt(e)
        if dt is None:
            e["invalido"] = True
            e["ignorado"] = True
            e["correccion"] = e.get("correccion") or "fechaHora_invalida"
            continue

        if t == "Entrada":
            if entrada_abierta_dt is None:
                entrada_abierta_dt = dt
            else:
                e["extra_entrada"] = True
                e["correccion"] = e.get("correccion") or "extra_entrada"
        else:  # Salida
            if entrada_abierta_dt is None:
                e["extra_salida"] = True
                e["correccion"] = e.get("correccion") or "extra_salida"
            else:
                hay_par_valido = True
                entrada_abierta_dt = None

    return eventos, hay_par_valido, entrada_abierta_dt


# ==========================================================
# Regla 5) Autocierre: genera salida sintética si aplica
# ==========================================================
def _autocierre_si_necesario(eventos: List[Dict], entrada_abierta_dt: datetime | None) -> Tuple[List[Dict], bool]:
    """
    Si hay entrada abierta y han pasado >= AUTO_CLOSE_UMBRAL_HORAS desde esa entrada
    según el último evento del día, agrega una "Salida" automática.
    """
    if entrada_abierta_dt is None:
        return eventos, False

    # "ahora" = última fechaHora válida (contable o no, pero con fecha parseable)
    fechas = []
    for e in eventos:
        dt = _safe_get_dt(e)
        if dt is not None:
            fechas.append(dt)
    if not fechas:
        return eventos, False

    ahora = max(fechas)
    if (ahora - entrada_abierta_dt) < timedelta(hours=AUTO_CLOSE_UMBRAL_HORAS):
        return eventos, False

    # Generar salida sintética
    if AUTO_CLOSE_MODO == "fin_jornada":
        salida_dt = _time_str_to_same_day(entrada_abierta_dt, HORA_FIN_JORNADA)
        if salida_dt < entrada_abierta_dt:
            salida_dt = entrada_abierta_dt + timedelta(hours=8)
    else:
        # Mantengo tu comportamiento original (entrada + 10h)
        salida_dt = entrada_abierta_dt + timedelta(hours=10)

    eventos.append({
        "tipo": "Salida",
        "fechaHora": salida_dt.isoformat(),
        "origen": "auto",
        "salida_automatica": True,
        "inferido": True,
        "correccion": "auto_close",
    })
    return eventos, True


# ==========================================================
# API principal
# ==========================================================
def sanear_eventos_dia(eventos: List[Dict]) -> Tuple[List[Dict], Dict]:
    """
    Recibe eventos de UN DÍA (idealmente ya solo Entrada/Salida).
    Aplica reglas en cadena para evitar que se rompa:

      0) ordenar + marcar inválidos
      1) clasificar desconocidos (ignorado)
      2) dedupe rebotes (duplicado)
      3) reclasificar salida inicial (si aplica)
      4) pairing (flags extra)
      5) autocierre (si aplica)

    Devuelve:
      (eventos_saneados, meta)

    meta:
      {
        "hizo_auto": bool,
        "hay_par_valido": bool,
        "entrada_abierta": bool
      }
    """
    eventos = _ordenar_y_marcar_invalidos(eventos or [])
    eventos = _clasificar_desconocidos(eventos)
    eventos = _dedupe_rebotes(eventos)
    eventos = _reclasificar_salida_inicial_si_corresponde(eventos)

    eventos, hay_par_valido, entrada_abierta_dt = _pairing(eventos)
    eventos, hizo_auto = _autocierre_si_necesario(eventos, entrada_abierta_dt)

    meta = {
        "hizo_auto": bool(hizo_auto),
        "hay_par_valido": bool(hay_par_valido),
        "entrada_abierta": bool(entrada_abierta_dt is not None),
    }
    return eventos, meta
