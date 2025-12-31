# servicios/asistencias_service.py
from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from pymongo import UpdateOne
from pymongo.errors import PyMongoError
from dateutil import tz
from zk import ZK

from servicios.mongo_service import conectar_mongo
from servicios.checador_service import PUERTO


# =========================
# Zona horaria y constantes
# =========================
TZ_LOCAL = tz.gettz("America/Mexico_City")

# Jornada laboral (para decidir Pendiente vs Salida Automática)
WORK_START_HH = 9
WORK_START_MM = 0
WORK_END_HH   = 18
WORK_END_MM   = 0


# =========================
# Helpers de tiempo
# =========================
def _parse_datetime_any(value) -> Optional[datetime]:
    """
    Convierte value a datetime con tz local:
      - datetime naive o aware
      - str en ISO: 'YYYY-MM-DD' o 'YYYY-MM-DDTHH:MM:SS[.mmm][+zz:zz]'
    Devuelve datetime **aware en zona local**.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TZ_LOCAL)
        return value.astimezone(TZ_LOCAL)

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_LOCAL)
            return dt.astimezone(TZ_LOCAL)
        except Exception:
            return None
    return None


def _to_utc(dt: datetime) -> datetime:
    """Convierte un datetime (naive local o tz-aware) a **UTC** tz-aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_LOCAL)
    return dt.astimezone(tz.UTC)


def _ensure_utc(value) -> Optional[datetime]:
    """
    Asegura que el valor sea datetime tz-aware en UTC.
    Acepta datetime (naive/aware) o str.
    """
    if isinstance(value, datetime):
        # Si viene naive, **asumimos** que ya estaba en UTC almacenado
        return value.replace(tzinfo=tz.UTC) if value.tzinfo is None else value.astimezone(tz.UTC)
    if isinstance(value, str):
        dt_loc = _parse_datetime_any(value)
        return _to_utc(dt_loc) if dt_loc else None
    return None


def _coerce_db_utc(value) -> Optional[datetime]:
    """
    Normaliza valores que vienen de Mongo ya guardados:
    - dict -> intentar extraer fechaHora dentro del dict
    - datetime naive -> asumir UTC
    - datetime aware -> devolver como UTC
    - str -> parsear y devolver UTC
    """
    # dicts del estilo {"$date": "..."} o {"fechaHora": ...}
    if isinstance(value, dict):
        for key in ("fechaHora", "datetime", "$date"):
            if key in value:
                return _coerce_db_utc(value[key])
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=tz.UTC)
        return value.astimezone(tz.UTC)

    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz.UTC)
            return dt.astimezone(tz.UTC)
        except Exception:
            return None

    return None


def _fecha_local_str(dt_utc: datetime) -> str:
    """Convierte UTC → 'YYYY-MM-DD' según America/Mexico_City."""
    return dt_utc.astimezone(TZ_LOCAL).date().isoformat()


# =========================
# Mapeos / normalización
# =========================
PUNCH_TO_TIPO = {0: "Entrada", 1: "Salida"}

def _tipo_desde_evento(ev) -> str:
    """
    Intenta mapear el tipo del evento del SDK a 'Entrada'/'Salida'.
    Soporta ev.punch o ev.status.
    """
    val = None
    for attr in ("punch", "status"):
        if hasattr(ev, attr):
            try:
                val = int(getattr(ev, attr))
                break
            except Exception:
                pass
    if val is not None and val in PUNCH_TO_TIPO:
        return PUNCH_TO_TIPO[val]
    return "Entrada"  # fallback conservador


def _normalizar_evento(ev, checador_ip: str) -> Tuple[str, Dict]:
    """
    Devuelve (trabajador_id, evento_dict normalizado).
    evento = {tipo, fechaHora(UTC), fechaHoraLocal(str), device_ip, origen, sincronizado}
    """
    trabajador = str(getattr(ev, "user_id", getattr(ev, "uid", "")))
    ts = getattr(ev, "timestamp", None) or getattr(ev, "time", None)
    if not trabajador or ts is None:
        return "", {}

    # Llevar timestamp a UTC tz-aware SIEMPRE
    if isinstance(ts, datetime):
        dt_utc = _to_utc(ts)
    elif isinstance(ts, str):
        dt_utc = _ensure_utc(ts)
        if not dt_utc:
            return "", {}
    else:
        return "", {}

    dt_local = dt_utc.astimezone(TZ_LOCAL)

    evento = {
        "tipo": _tipo_desde_evento(ev),
        "fechaHora": dt_utc,                                  # UTC para cálculo/backend
        "fechaHoraLocal": dt_local.strftime("%Y-%m-%d %H:%M:%S"),  # legible en Mongo
        "device_ip": checador_ip,
        "origen": "zkteco",
        "sincronizado": False,
    }
    return trabajador, evento


def _dedupe_eventos(eventos: List[Dict]) -> List[Dict]:
    """Elimina duplicados exactos por (tipo, fechaHora)."""
    seen = set()
    out = []
    for e in eventos:
        key = (e.get("tipo"), e.get("fechaHora"))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# =========================
# Mongo / ZK helpers
# =========================
def _ensure_indexes(db):
    """Índices recomendados."""
    db.asistencias.create_index(
        [("trabajador", 1), ("sede", 1), ("fecha", 1)],
        name="idx_trabajador_sede_fecha",
        unique=False  # cámbialo a True cuando limpies la colección
    )
    db.asistencias.create_index([("detalle.fechaHora", 1)], name="idx_detalle_fechaHora")


def _leer_eventos_zk(checador_ip: str) -> List:
    """Conecta al checador y devuelve la lista de eventos de asistencia."""
    zk = ZK(checador_ip, port=PUERTO, timeout=8)
    conn = zk.connect()
    try:
        fn = getattr(conn, "get_attendance", None) or getattr(conn, "get_attendances", None)
        if not fn:
            raise RuntimeError("El SDK no expone get_attendance(s).")
        eventos = fn()
        return eventos or []
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


# =========================
# DEBUG helpers
# =========================
def _debug_dump_event_window(normalizados: List[Tuple[str, Dict]], log: Callable[[str], None]) -> None:
    if not normalizados:
        log("🟡 No hay eventos normalizados.")
        return
    fechas_local = []
    for t, e in normalizados[:10]:  # muestra 10 para no saturar
        utc = e["fechaHora"]
        loc = utc.astimezone(TZ_LOCAL)
        fechas_local.append(loc.date().isoformat())
        log(f"   · uid={t} UTC={utc.isoformat()}  LOCAL={loc.strftime('%Y-%m-%d %H:%M:%S')}")
    fechas = sorted(set(fechas_local))
    log(f"📅 Fechas LOCAL detectadas en muestra: {fechas}")


def _filtrar_solo_hoy(normalizados: List[Tuple[str, Dict]]) -> List[Tuple[str, Dict]]:
    hoy = datetime.now(TZ_LOCAL).date()
    out = []
    for t, e in normalizados:
        loc_date = e["fechaHora"].astimezone(TZ_LOCAL).date()
        if loc_date == hoy:
            out.append((t, e))
    return out


# =========================
# Estado del día
# =========================
def _construye_estado_y_flags(detalle_ordenado: List[Dict]) -> Tuple[str, bool, bool]:
    """
    Devuelve (estado, salida_automatica, salida_registrada) con regla:
      - Entrada y Salida -> Asistencia Completa
      - Entrada sin Salida:
          * Si es HOY y aún no termina la jornada (18:00 local) -> Pendiente
          * Si ya terminó la jornada HOY o es un día anterior -> Asistencia con salida automática
      - Sin Entrada -> Sin asistencia
    """
    # Normalizar TODAS las fechas del detalle a datetime UTC seguro
    detalle_norm: List[Dict] = []
    for it in (detalle_ordenado or []):
        fh = it.get("fechaHora")
        if isinstance(fh, datetime):
            fh_utc = fh.astimezone(tz.UTC)
        else:
            fh_utc = _coerce_db_utc(fh)
        if fh_utc is None:
            # Si no se puede normalizar, lo saltamos
            continue
        obj = dict(it)
        obj["fechaHora"] = fh_utc
        detalle_norm.append(obj)

    tiene_entrada = any(it.get("tipo") == "Entrada" for it in detalle_norm)
    tiene_salida  = any(it.get("tipo") == "Salida"  for it in detalle_norm)

    if tiene_entrada and tiene_salida:
        return "Asistencia Completa", False, True

    if tiene_entrada and not tiene_salida:
        # Tomar la primera entrada en UTC y llevarla a local para el cálculo
        entradas_utc = [it["fechaHora"] for it in detalle_norm if it.get("tipo") == "Entrada"]
        if not entradas_utc:
            return "Asistencia con salida automática", True, False

        first_dt_local = min(entradas_utc).astimezone(TZ_LOCAL)

        hoy_local   = datetime.now(TZ_LOCAL).date()
        ahora_local = datetime.now(TZ_LOCAL)
        fin_jornada = first_dt_local.replace(
            hour=WORK_END_HH, minute=WORK_END_MM, second=0, microsecond=0
        )

        if first_dt_local.date() == hoy_local:
            if ahora_local < fin_jornada:
                return "Pendiente", False, False
            else:
                return "Asistencia con salida automática", True, False
        else:
            return "Asistencia con salida automática", True, False

    return "Sin asistencia", False, False


def _merge_detalle_existente_y_nuevo(det_actual: List[Dict], det_nuevo: List[Dict]) -> List[Dict]:
    """
    Fusión idempotente por (tipo, fechaHora), ordenado por fechaHora asc.
    Asegura que 'fechaHora' sea datetime tz-aware en UTC para evitar comparaciones mixtas.
    """
    def normalize_item(it: Dict) -> Dict:
        fh = it.get("fechaHora")
        fixed = _coerce_db_utc(fh)
        if fixed is not None:
            it = dict(it)  # no mutar el original
            it["fechaHora"] = fixed
        return it

    det_actual_norm = [normalize_item(x) for x in (det_actual or [])]
    det_nuevo_norm  = [normalize_item(x) for x in (det_nuevo  or [])]

    def key(it):
        return (it.get("tipo"), it.get("fechaHora"))

    merged = {key(it): it for it in det_actual_norm}
    for it in det_nuevo_norm:
        merged[key(it)] = it  # sobreescribe si es el mismo (idempotente)

    def sort_key(it: Dict) -> float:
        fh = it.get("fechaHora")
        if not isinstance(fh, datetime) or fh.tzinfo is None:
            fh = _coerce_db_utc(fh) or datetime.min.replace(tzinfo=tz.UTC)
        return fh.timestamp()

    return sorted(merged.values(), key=sort_key)


# =========================
# API principal
# =========================
def obtener_asistencias(
    sede_id: int,
    checador_ip: str,
    desde: Optional[datetime | str] = None,
    hasta: Optional[datetime | str] = None,
    log_fn: Optional[Callable[[str], None]] = None
) -> Dict:
    """
    Lee eventos del checador y los persiste en Mongo agrupados por día/trabajador con
    la estructura:

    {
      "trabajador": "105",
      "sede": 1,
      "fecha": "YYYY-MM-DD",
      "detalle": [
        {
          "tipo": "Entrada" | "Salida",
          "fechaHora": <UTC datetime>,
          "fechaHoraLocal": "YYYY-MM-DD HH:MM:SS",
          "device_ip": "192.168.1.101",
          "origen": "zkteco",
          "sincronizado": false,
          "trabajador": "105",
          "sede": 1
        }
      ],
      "estado": "Asistencia Completa" | "Asistencia con salida automática" | "Pendiente" | "Sin asistencia",
      "salida_automatica": <bool>,
      "salida_registrada": <bool>
    }
    """
    def log(msg: str):
        (log_fn or print)(msg)

    # 1) Mongo
    cliente, msg = conectar_mongo()
    if not cliente:
        raise RuntimeError("No se pudo conectar a MongoDB: " + msg)
    db = cliente["Registro_Alu"]
    _ensure_indexes(db)

    # 2) Checador
    log(f"🔌 Leyendo eventos desde checador {checador_ip}…")
    crudos = _leer_eventos_zk(checador_ip)
    log(f"📦 Recibidos {len(crudos)} evento(s) crudos.")

    # 3) Normalizar
    normalizados: List[Tuple[str, Dict]] = []
    for ev in crudos:
        t, e = _normalizar_evento(ev, checador_ip)
        if t and e:
            normalizados.append((t, e))
    log(f"🧰 Normalizados {len(normalizados)} evento(s).")

    # 3.5) Blindaje: forzar que todos los fechaHora queden como datetime UTC
    fixed: List[Tuple[str, Dict]] = []
    for t, e in normalizados:
        fh = e.get("fechaHora")
        if isinstance(fh, datetime):
            fh_utc = fh.astimezone(tz.UTC)
        else:
            fh_utc = _coerce_db_utc(fh)
        if not fh_utc:
            continue
        e2 = dict(e)
        e2["fechaHora"] = fh_utc
        fixed.append((t, e2))
    normalizados = fixed

    log(f"🔍 Tras normalizar: {len(normalizados)} evento(s). Muestra:")
    _debug_dump_event_window(normalizados, log)

    # 👉 Descomenta para procesar SOLO eventos de HOY (LOCAL) durante pruebas
    # normalizados = _filtrar_solo_hoy(normalizados)
    # log(f"🎯 Solo HOY (local): {len(normalizados)} evento(s) tras filtro.")

    # 4) Filtrar por rango (si se pasó desde/hasta) – SIEMPRE en UTC tz-aware
    if desde:
        d_utc = _ensure_utc(desde)
        if d_utc:
            normalizados = [(t, e) for (t, e) in normalizados
                            if isinstance(e.get("fechaHora"), datetime) and e["fechaHora"] >= d_utc]

    if hasta:
        h_utc = _ensure_utc(hasta)
        if h_utc:
            normalizados = [(t, e) for (t, e) in normalizados
                            if isinstance(e.get("fechaHora"), datetime) and e["fechaHora"] <= h_utc]

    log(f"⏱️ Después de rango, quedan {len(normalizados)} evento(s).")

    if not normalizados:
        return {"ok": True, "insertados": 0, "actualizados": 0, "eventos": 0}

    # 5) Agrupar por (trabajador, fecha_local) + redundancia en cada item
    agrupado: Dict[Tuple[str, str], List[Dict]] = {}
    conteo_por_fecha: Dict[str, int] = {}  # debug
    for trabajador, evento in normalizados:
        # Asegurar UTC siempre
        if not isinstance(evento.get("fechaHora"), datetime):
            fixed_dt = _ensure_utc(evento.get("fechaHora"))
            if not fixed_dt:
                continue
            evento["fechaHora"] = fixed_dt

        fecha_local = _fecha_local_str(evento["fechaHora"])
        conteo_por_fecha[fecha_local] = conteo_por_fecha.get(fecha_local, 0) + 1  # debug

        evento_red = dict(evento)
        evento_red["trabajador"] = trabajador
        evento_red["sede"] = int(sede_id)

        agrupado.setdefault((trabajador, fecha_local), []).append(evento_red)

    # DEBUG: muestra distribución por fecha local
    log("🧭 Distribución por fecha (LOCAL) en esta corrida:")
    for f, c in sorted(conteo_por_fecha.items()):
        log(f"   · {f}: {c} evento(s)")

    # 6) Find+Merge+Update por grupo (para calcular estado/flags)
    col = db.asistencias
    insertados = 0
    actualizados = 0
    total_eventos = 0
    ops: List[UpdateOne] = []

    for (trabajador, fecha_local), eventos in agrupado.items():
        eventos = _dedupe_eventos(eventos)
        total_eventos += len(eventos)

        filtro = {"trabajador": trabajador, "sede": int(sede_id), "fecha": fecha_local}
        actual = col.find_one(filtro, {"_id": 0, "detalle": 1}) or {}
        detalle_actual = actual.get("detalle", [])

        # Merge sin duplicados (asegura datetime UTC dentro)
        detalle_merged = _merge_detalle_existente_y_nuevo(detalle_actual, eventos)

        # estado + flags
        estado, salida_auto, salida_reg = _construye_estado_y_flags(detalle_merged)

        update = {
            "$set": {
                "trabajador": trabajador,
                "sede": int(sede_id),
                "fecha": fecha_local,
                "detalle": detalle_merged,
                "estado": estado,
                "salida_automatica": salida_auto,
                "salida_registrada": salida_reg,
            }
        }
        ops.append(UpdateOne(filtro, update, upsert=True))

    # 7) Bulk write
    try:
        res = col.bulk_write(ops, ordered=False)
        insertados = res.upserted_count
        actualizados = res.modified_count
        log(f"🧾 Bulk resultado: upserted={insertados}, modified={actualizados}, matched={res.matched_count}")
        return {"ok": True, "insertados": insertados, "actualizados": actualizados, "eventos": total_eventos}
    except PyMongoError as e:
        log(f"❌ Error escribiendo en Mongo: {e}")
        raise


# =========================
# Limpieza del dispositivo (opcional)
# =========================
def limpiar_asistencias_checador(
    checador_ip: str,
    log_fn: Optional[Callable[[str], None]] = None
) -> Dict:
    """
    Borra (reset) los logs de asistencia en el dispositivo (NO toca Mongo).
    Úsalo solo si ya confirmaste que todo está guardado en DB.
    """
    def log(msg: str):
        (log_fn or print)(msg)

    zk = ZK(checador_ip, port=PUERTO, timeout=8)
    conn = zk.connect()
    try:
        fn = getattr(conn, "clear_attendance", None) or getattr(conn, "clear_attendances", None)
        if not fn:
            raise RuntimeError("El SDK no expone clear_attendance(s).")
        fn()
        log("🧹 Asistencias borradas del dispositivo.")
        return {"ok": True}
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass
