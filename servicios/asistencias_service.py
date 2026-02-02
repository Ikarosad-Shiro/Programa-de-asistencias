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
from asistencias_rules import sanear_eventos_dia

#from servicios.asistencias_rules import sanear_eventos_dia

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
            value = value.replace("Z", "+00:00") 
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
    Asegura datetime tz-aware en UTC.
    - datetime naive: se asume LOCAL (America/Mexico_City) y se convierte a UTC
    - datetime aware: se convierte a UTC
    - str: se parsea y se convierte a UTC
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ_LOCAL)
        return value.astimezone(tz.UTC)

    if isinstance(value, str):
        dt_loc = _parse_datetime_any(value)
        return dt_loc.astimezone(tz.UTC) if dt_loc else None

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
            value = value.replace("Z", "+00:00")
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

VALID_TIPOS = {"Entrada", "Salida"}

def _mapear_a_entrada_salida(it: Dict) -> Dict:
    """
    Fuerza que tipo sea SOLO Entrada/Salida.
    Si viene otro valor, lo marca como Desconocido (interno) para inferir luego.
    """
    t = it.get("tipo")

    if t in VALID_TIPOS:
        return it

    it2 = dict(it)
    it2["tipo_original"] = t
    it2["tipo"] = "Desconocido"   # marcador interno
    it2["inferido"] = True
    it2["correccion"] = it2.get("correccion") or "tipo_fuera_de_catalogo"
    return it2

def _normalizar_tipos_por_alternancia(detalle: List[Dict]) -> Tuple[List[Dict], bool]:
    """
    Normaliza para que TODOS los eventos terminen con tipo Entrada/Salida.
    Corrige:
      - tipo Desconocido -> se convierte por alternancia
      - modo fijo (Entrada, Entrada, Entrada...) -> alterna
    Devuelve (detalle_normalizado, hubo_correcciones)
    """
    if not detalle:
        return detalle, False

    # 1) Marca tipos fuera de catálogo
    det = [_mapear_a_entrada_salida(x) for x in detalle]

    # 2) Ordena por fechaHora (UTC preferible)
    def _ts(it: Dict) -> float:
        fh = it.get("fechaHora")
        if isinstance(fh, datetime):
            if fh.tzinfo is None:
                fh = fh.replace(tzinfo=tz.UTC)
            return fh.timestamp()
        fh2 = _coerce_db_utc(fh)
        return (fh2 or datetime.min.replace(tzinfo=tz.UTC)).timestamp()

    det.sort(key=_ts)

    # 3) Alternancia
    hubo = False
    last_tipo = None

    # Encuentra el primer tipo válido como base (si no hay, asumimos que empieza con Entrada)
    for it in det:
        if it.get("tipo") in VALID_TIPOS:
            last_tipo = it["tipo"]
            break
    if last_tipo is None:
        last_tipo = "Salida"  # para que el primer esperado sea Entrada

    out = []
    for it in det:
        t = it.get("tipo")
        esperado = "Salida" if last_tipo == "Entrada" else "Entrada"

        # Caso A: Desconocido -> esperado
        if t == "Desconocido":
            it2 = dict(it)
            it2["tipo"] = esperado
            it2["inferido"] = True
            it2["correccion"] = it2.get("correccion") or "inferido_por_historial"
            out.append(it2)
            last_tipo = esperado
            hubo = True
            continue

        # Caso B: válido pero repetido -> corregir
        if t in VALID_TIPOS and t == last_tipo:
            it2 = dict(it)
            it2["tipo_original"] = it2.get("tipo_original", t)
            it2["tipo"] = esperado
            it2["inferido"] = True
            it2["correccion"] = "modo_fijo_corregido"
            out.append(it2)
            last_tipo = esperado
            hubo = True
            continue

        # Caso C: válido y alterna bien
        if t in VALID_TIPOS:
            out.append(it)
            last_tipo = t
            continue

        # Cualquier cosa rara (no debería llegar): forzar a esperado
        it2 = dict(it)
        it2["tipo_original"] = it2.get("tipo_original", t)
        it2["tipo"] = esperado
        it2["inferido"] = True
        it2["correccion"] = it2.get("correccion") or "fallback_forzado"
        out.append(it2)
        last_tipo = esperado
        hubo = True

    return out, hubo

# =========================
# API principal
# =========================
def obtener_asistencias(
    sede_id: int,
    checador_ip: str,
    desde: Optional[datetime | str] = None,
    hasta: Optional[datetime | str] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    subir_a_mongo: bool = True,   # 👈 NUEVO
) -> Dict:
    def log(msg: str):
        (log_fn or print)(msg)

    # 1) Checador (SIEMPRE)
    log(f"🔌 Leyendo eventos desde checador {checador_ip}…")
    crudos = _leer_eventos_zk(checador_ip)
    log(f"📦 Recibidos {len(crudos)} evento(s) crudos.")

    # 2) Normalizar
    normalizados: List[Tuple[str, Dict]] = []
    for ev in crudos:
        t, e = _normalizar_evento(ev, checador_ip)
        if t and e:
            normalizados.append((t, e))
    log(f"🧰 Normalizados {len(normalizados)} evento(s).")

    # 2.5 Blindaje fechaHora -> datetime UTC
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

    # 3) Rango (UTC)
    if desde:
        d_utc = _ensure_utc(desde)
        if d_utc:
            normalizados = [(t, e) for (t, e) in normalizados if e["fechaHora"] >= d_utc]

    if hasta:
        h_utc = _ensure_utc(hasta)
        if h_utc:
            # OJO: en tu autosync pasas "mañana 00:00" como EXCLUSIVO,
            # entonces aquí conviene usar < y no <=
            normalizados = [(t, e) for (t, e) in normalizados if e["fechaHora"] < h_utc]

    log(f"⏱️ Después de rango, quedan {len(normalizados)} evento(s).")
    if not normalizados:
        return {"ok": True, "insertados": 0, "actualizados": 0, "eventos": 0, "documentos": []}

    # 4) Agrupar por (trabajador, fecha_local)
    agrupado: Dict[Tuple[str, str], List[Dict]] = {}
    conteo_por_fecha: Dict[str, int] = {}
    for trabajador, evento in normalizados:
        fecha_local = _fecha_local_str(evento["fechaHora"])
        conteo_por_fecha[fecha_local] = conteo_por_fecha.get(fecha_local, 0) + 1

        evento_red = dict(evento)
        evento_red["trabajador"] = trabajador
        evento_red["sede"] = int(sede_id)
        agrupado.setdefault((trabajador, fecha_local), []).append(evento_red)

    log("🧭 Distribución por fecha (LOCAL) en esta corrida:")
    for f, c in sorted(conteo_por_fecha.items()):
        log(f"   · {f}: {c} evento(s)")

    # 5) Construir documentos (sin Mongo) o upsert (con Mongo)
    documentos: List[Dict] = []
    total_eventos = 0

    if not subir_a_mongo:
        # ---- MODO SOLO LECTURA: NO TOCA MONGO ----
        for (trabajador, fecha_local), eventos in agrupado.items():
            eventos = _dedupe_eventos(eventos)
            total_eventos += len(eventos)

            # Ordenar por fechaHora
            eventos = sorted(eventos, key=lambda x: x["fechaHora"].timestamp())

            # Normalización + rules también en modo lectura
            detalle_merged = eventos
            detalle_norm, hubo_norm = _normalizar_tipos_por_alternancia(detalle_merged)
            detalle_saneado, _meta = sanear_eventos_dia(detalle_norm)
            detalle_saneado = _merge_detalle_existente_y_nuevo([], detalle_saneado)

            estado, salida_auto, salida_reg = _construye_estado_y_flags(detalle_saneado)
            hubo_inferidos = hubo_norm or any(e.get("inferido") for e in detalle_saneado)

            documentos.append({
                "trabajador": trabajador,
                "sede": int(sede_id),
                "fecha": fecha_local,
                "detalle": detalle_saneado,
                "requiere_revision": bool(hubo_inferidos),
                "estado": estado,
                "salida_automatica": salida_auto,
                "salida_registrada": salida_reg,
            })

        log(f"📄 Generados {len(documentos)} documento(s) en memoria (sin subir a Mongo).")
        return {
            "ok": True,
            "insertados": 0,
            "actualizados": 0,
            "eventos": total_eventos,
            "documentos": documentos,  # 👈 clave para respaldos
            "subido_a_mongo": False
        }

    # ---- MODO NORMAL: con Mongo ----
    cliente, msg = conectar_mongo()
    if not cliente:
        raise RuntimeError("No se pudo conectar a MongoDB: " + msg)
    db = cliente["Registro_Alu"]
    _ensure_indexes(db)
    col = db.asistencias

    ops: List[UpdateOne] = []
    for (trabajador, fecha_local), eventos in agrupado.items():
        eventos = _dedupe_eventos(eventos)
        total_eventos += len(eventos)

        filtro = {"trabajador": trabajador, "sede": int(sede_id), "fecha": fecha_local}
        actual = col.find_one(filtro, {"_id": 0, "detalle": 1}) or {}
        detalle_actual = actual.get("detalle", [])

        detalle_merged = _merge_detalle_existente_y_nuevo(detalle_actual, eventos)

        # ✅ 1) Normalización estricta: SOLO Entrada/Salida + corregir modo fijo
        detalle_norm, hubo_norm = _normalizar_tipos_por_alternancia(detalle_merged)

        # ✅ 2) Aplicar tus reglas del día (rebotes, salida inicial, pairing, autocierre)
        detalle_saneado, _ = sanear_eventos_dia(detalle_norm)

        # ✅ 3) Re-normalizar por si rules agregó salida automática como string
        detalle_saneado = _merge_detalle_existente_y_nuevo([], detalle_saneado)

        # ✅ 4) Estado final (Pendiente solo HOY)
        estado, salida_auto, salida_reg = _construye_estado_y_flags(detalle_saneado)

        # ✅ 5) Marca auditoría si hubo correcciones (normalización o rules)
        hubo_inferidos = hubo_norm or any(e.get("inferido") for e in detalle_saneado)



        update = {"$set": {
            "trabajador": trabajador,
            "sede": int(sede_id),
            "fecha": fecha_local,
            "detalle": detalle_saneado,
            "estado": estado,
            "salida_automatica": salida_auto,
            "salida_registrada": salida_reg,
            "requiere_revision": bool(hubo_inferidos),
        }}
        log(f"🧠 {trabajador} {fecha_local}: merged={len(detalle_merged)} norm={len(detalle_norm)} saneado={len(detalle_saneado)} estado={estado} revision={hubo_inferidos}")
        ops.append(UpdateOne(filtro, update, upsert=True))

    try:
        res = col.bulk_write(ops, ordered=False)
        log(f"🧾 Bulk resultado: upserted={res.upserted_count}, modified={res.modified_count}, matched={res.matched_count}")
        return {"ok": True, "insertados": res.upserted_count, "actualizados": res.modified_count, "eventos": total_eventos, "subido_a_mongo": True}
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
