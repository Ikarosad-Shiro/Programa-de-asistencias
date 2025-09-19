# servicios/trabajadores_service.py
import json
import os
import socket
from datetime import datetime
from pymongo import UpdateOne
from pymongo.errors import PyMongoError
from zk import ZK
from typing import Set

from servicios.mongo_service import conectar_mongo
from servicios.checador_service import IPS, PUERTO  # usamos tu listado de IPs y puerto

# 🔒 1..100 reservado (admins/servicio/etc.)
RANGO_RESERVADO_MAX = 99  # trabajadores válidos: id_checador > 100

def _cargar_config():
    if not os.path.exists("configuracion_temporal.json"):
        return None
    with open("configuracion_temporal.json", "r", encoding="utf-8") as f:
        return json.load(f)

def agregar_trabajadores_de_sede(sede_id=None, log_fn=print):
    """
    Agrega al checador los trabajadores de la sede dada que:
      - estado == 'activo'
      - id_checador > 100 (evita rango reservado 1..100)
      - aún no existen en el checador
    
    Además, marca sincronizado=True en Mongo tanto si se agregan como si ya existían en el checador.

    sede_id: int | None  → si es None, se toma de configuracion_temporal.json
    log_fn:  callable    → función para log (por defecto print)
    Retorna: dict(resumen) con agregados, saltados, marcados y errores
    """
    # ✅ Asegurar logger válido
    if not callable(log_fn):
        log_fn = print

    # ✅ Sede desde arg o desde la config temporal
    if sede_id is None:
        cfg = _cargar_config()
        if not cfg:
            log_fn("❌ No se encontró configuracion_temporal.json")
            return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 1}
        sede_id = cfg.get("sede")
        if sede_id is None:
            log_fn("❌ La configuración no contiene 'sede'.")
            return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 1}

    # 1) Conectar a Mongo
    cliente, msg = conectar_mongo()
    if not cliente:
        log_fn(f"❌ MongoDB: {msg}")
        return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 1}

    db = cliente["Registro_Alu"]
    col_trab = db["trabajadores"]

    # 2) Obtener trabajadores válidos de la sede
    try:
        filtro = {
            "sede": int(sede_id),
            "estado": "activo",
            "id_checador": {"$gt": RANGO_RESERVADO_MAX}  # > 100
        }
        campos = {"_id": 1, "id_checador": 1, "nombre": 1, "sincronizado": 1}
        trabajadores = list(col_trab.find(filtro, campos))
    except PyMongoError as e:
        log_fn(f"❌ Error consultando trabajadores: {e}")
        return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 1}

    if not trabajadores:
        log_fn(f"ℹ️ No hay trabajadores activos con id_checador > {RANGO_RESERVADO_MAX} para la sede {sede_id}.")
        return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 0}

    # 3) Conectar al checador recorriendo IPs conocidas
    conn = None
    checador_ip = None
    for ip in IPS:
        try:
            log_fn(f"🔌 Intentando conectar con checador {ip}:{PUERTO}...")
            zk = ZK(ip, port=PUERTO, timeout=8)
            conn = zk.connect()
            checador_ip = ip
            log_fn(f"✅ Conectado a checador {ip}")
            break
        except (socket.timeout, socket.error) as e:
            log_fn(f"🌐 {ip}:{PUERTO} no disponible → {e}")
        except Exception as e:
            log_fn(f"🧨 Error conectando a {ip}:{PUERTO} → {e}")

    if not conn:
        log_fn("❌ No fue posible conectar a ningún checador.")
        return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 1}

    # 4) Obtener usuarios actuales del checador
    try:
        actuales = conn.get_users()
        ids_checador = {int(u.user_id) for u in actuales}
    except Exception as e:
        log_fn(f"❌ Error obteniendo usuarios del checador: {e}")
        try:
            conn.disconnect()
        except Exception:
            pass
        return {"agregados": 0, "saltados": 0, "marcados": 0, "errores": 1}

    log_fn(f"📡 Checador en {checador_ip}: {len(ids_checador)} usuarios existentes.")

    agregados = 0
    saltados = 0
    marcados = 0  # ← nuevos: cuántos solo se marcaron como sincronizados
    errores = 0
    ops = []  # updates en Mongo

    # 5) Procesar trabajadores
    for t in trabajadores:
        uid = int(t["id_checador"])
        nombre = (t.get("nombre") or "").strip()

        if uid <= RANGO_RESERVADO_MAX:
            log_fn(f"⛔ ID {uid} está en el rango reservado 1..{RANGO_RESERVADO_MAX}. Saltando.")
            saltados += 1
            continue

        # Si YA existe en checador, no lo agregamos, pero marcamos sincronizado en Mongo si hiciera falta
        if uid in ids_checador:
            saltados += 1
            if not t.get("sincronizado", False):
                ops.append(UpdateOne(
                    {"_id": t["_id"]},
                    {"$set": {"sincronizado": True, "ultima_sincronizacion": datetime.utcnow()}}
                ))
                marcados += 1
                log_fn(f"🔵 Ya estaba en checador: {uid} - {nombre}. Marcado como sincronizado en Mongo.")
            else:
                log_fn(f"➡️ Ya existe en checador: {uid} - {nombre}. (Sin cambios)")
            continue

        # Si NO existe en checador → agregar
        nombre_limpio = nombre[:24]  # muchos ZK limitan el nombre aprox a 24 chars

        try:
            conn.set_user(
                uid=uid,
                name=nombre_limpio,
                privilege=0,
                password="",
                group_id="",
                user_id=str(uid)
            )
            agregados += 1
            log_fn(f"✅ Agregado: {uid} - {nombre_limpio}")

            # marcar sincronizado en Mongo
            ops.append(UpdateOne(
                {"_id": t["_id"]},
                {"$set": {"sincronizado": True, "ultima_sincronizacion": datetime.utcnow()}}
            ))
        except Exception as e:
            errores += 1
            log_fn(f"❌ Error al agregar {uid} - {nombre_limpio}: {e}")

    # 6) Guardar marca de sincronización en lote
    if ops:
        try:
            col_trab.bulk_write(ops, ordered=False)
        except Exception as e:
            log_fn(f"⚠️ No se pudo actualizar 'sincronizado' en Mongo: {e}")

    try:
        conn.disconnect()
    except Exception:
        pass

    resumen = {"agregados": agregados, "saltados": saltados, "marcados": marcados, "errores": errores}
    log_fn(f"📊 Resumen → {resumen}")
    return resumen

# --- Limpieza automática: elimina del checador lo que NO es de la sede ni admin ---

def _ids_permitidos_por_sede(sede_id: int, log_fn=print) -> Set[int]:
    if not callable(log_fn):
        log_fn = print
    cliente, msg = conectar_mongo()
    if not cliente:
        log_fn(f"❌ MongoDB: {msg}")
        return set()
    db = cliente["Registro_Alu"]
    col_trab = db["trabajadores"]
    try:
        filtro = {
            "sede": int(sede_id),
            "estado": "activo",
            "id_checador": {"$gt": RANGO_RESERVADO_MAX}  # >99
        }
        campos = {"_id": 0, "id_checador": 1}
        ids = []
        for doc in col_trab.find(filtro, campos):
            try:
                ids.append(int(doc["id_checador"]))
            except Exception:
                continue
        return set(ids)
    except PyMongoError as e:
        log_fn(f"❌ Error consultando trabajadores por sede: {e}")
        return set()

def eliminar_trabajadores_no_sede(
    sede_id=None,
    log_fn=print,
    dry_run=True,
    show_details=True,
    preset_candidatos=None
):
    """
    Limpia el checador eliminando usuarios que:
      - NO estén en Mongo como activos de la sede con id_checador > RANGO_RESERVADO_MAX
      - Y tengan privilege == 0 (usuario normal)
      - Además, si uid <= RANGO_RESERVADO_MAX y privilege == 0 → eliminar (no debería existir)

    Conserva siempre:
      - Cualquiera con privilege != 0 (admin/supervisor)
      - Quienes estén en la sede actual (Mongo)

    Parámetros:
      - dry_run: si True, NO elimina; solo muestra candidatos.
      - show_details: si True, imprime admins, lista de sede con nombres, etc.
      - preset_candidatos: lista de IDs ya calculados (para aplicar sin repetir logs).
    """
    if not callable(log_fn):
        log_fn = print

    # 1) Sede
    if sede_id is None:
        cfg = _cargar_config()
        if not cfg:
            log_fn("❌ No se encontró configuracion_temporal.json")
            return {"eliminados": [], "candidatos": [], "admins": [], "sede": [], "total_actuales": 0, "errores": 1}
        sede_id = cfg.get("sede")
        if sede_id is None:
            log_fn("❌ La configuración no contiene 'sede'.")
            return {"eliminados": [], "candidatos": [], "admins": [], "sede": [], "total_actuales": 0, "errores": 1}
    sede_id = int(sede_id)

    # Si nos pasan candidatos precomputados, saltamos el escaneo detallado
    if preset_candidatos is not None:
        try:
            candidatos = list(sorted(set(int(x) for x in preset_candidatos)))
        except Exception:
            candidatos = []
        admins = []
        de_sede = []
        usuarios = [None] * len(candidatos)
        checador_ip = "preset"
        show_details = False  # no mostramos detalle en la ejecución real
    else:
        # 2) IDs permitidos (desde Mongo)
        log_fn(f"🔎 Consultando IDs permitidos (sede {sede_id}) en Mongo…")
        ids_permitidos = _ids_permitidos_por_sede(sede_id, log_fn=log_fn)
        if show_details:
            log_fn(f"✅ IDs de la sede: {sorted(ids_permitidos) if ids_permitidos else '—'}")

        # 3) Conectar al checador (recorriendo IPs conocidas)
        conn = None
        checador_ip = None
        zk = None
        for ip in IPS:
            try:
                log_fn(f"🔌 Intentando checador {ip}:{PUERTO}…")
                zk = ZK(ip, port=PUERTO, timeout=8)
                conn = zk.connect()
                checador_ip = ip
                log_fn(f"✅ Conectado a {ip}")
                break
            except (socket.timeout, socket.error) as e:
                if show_details:
                    log_fn(f"🌐 {ip}:{PUERTO} no disponible → {e}")
            except Exception as e:
                if show_details:
                    log_fn(f"🧨 Error conectando a {ip}:{PUERTO} → {e}")

        if not conn:
            log_fn("❌ No fue posible conectar a ningún checador.")
            return {"eliminados": [], "candidatos": [], "admins": [], "sede": [], "total_actuales": 0, "errores": 1}

        # 4) Leer usuarios del checador
        try:
            conn.disable_device()
            usuarios = conn.get_users()
        except Exception as e:
            log_fn(f"❌ Error leyendo usuarios: {e}")
            try:
                conn.enable_device(); conn.disconnect()
            except Exception:
                pass
            return {"eliminados": [], "candidatos": [], "admins": [], "sede": [], "total_actuales": 0, "errores": 1}

        admins = []
        de_sede = []
        candidatos = []

        for u in usuarios:
            try:
                uid = int(u.user_id)
            except Exception:
                continue
            privilege = getattr(u, "privilege", 0)

            # Mantener si es de la sede
            if uid in ids_permitidos:
                de_sede.append(uid)
                continue

            # Mantener si es admin/supervisor
            if privilege != 0:
                admins.append(uid)
                continue

            # Seguridad: si está en 1..99 y privilege==0 → eliminar
            if uid <= RANGO_RESERVADO_MAX and privilege == 0:
                candidatos.append(uid)
                continue

            # Usuario normal fuera de la sede → eliminar
            candidatos.append(uid)

        # Logs de detalle (solo en preview)
        if show_details:
            log_fn(f"📋 Checador {checador_ip}: total usuarios = {len(usuarios)}")
            log_fn(f"🛡️ Admins detectados (privilege!=0): {sorted(admins) if admins else '—'}")

            # 4.1) Imprimir los nombres de los IDs de la sede (bonito)
            if de_sede:
                log_fn("🏷️ IDs pertenecientes a la sede:")
                try:
                    cliente, msg = conectar_mongo()
                    if cliente is not None:
                        db = cliente["Registro_Alu"]
                        col_trab = db["trabajadores"]
                        for sid in sorted(de_sede):
                            info = col_trab.find_one({"id_checador": sid}, {"nombre": 1})
                            if info and info.get("nombre"):
                                log_fn(f"   ➡️ {sid} - {info['nombre']}")
                            else:
                                log_fn(f"   ➡️ {sid} - (Sin nombre en Mongo)")
                    else:
                        for sid in sorted(de_sede):
                            log_fn(f"   ➡️ {sid} - (No se pudo conectar a Mongo)")
                except Exception as e:
                    log_fn(f"⚠️ Error consultando nombres en Mongo: {e}")
            else:
                log_fn("🏷️ IDs pertenecientes a la sede: —")

            # 4.2) Candidatos
            log_fn(f"🗑️ Candidatos a eliminar: {sorted(candidatos) if candidatos else '—'}")

        # Si es DRY-RUN o no hay candidatos → cerrar conexión y devolver
        if dry_run or not candidatos:
            if dry_run and show_details:
                log_fn("🧪 DRY-RUN o sin candidatos: no se elimina nada.")
            try:
                conn.enable_device(); conn.disconnect()
            except Exception:
                pass
            return {
                "eliminados": [],
                "candidatos": sorted(candidatos),
                "admins": sorted(admins),
                "sede": sorted(de_sede),
                "total_actuales": len(usuarios),
                "errores": 0
            }

        # Si SÍ vamos a eliminar (y no venimos con preset), dejamos conexión abierta para borrar
        col_trab = None
        try:
            cliente, msg = conectar_mongo()
            if cliente:
                db = cliente["Registro_Alu"]
                col_trab = db["trabajadores"]
        except Exception:
            col_trab = None

        eliminados = []
        errores = 0

        for uid in sorted(set(candidatos)):
            try:
                # Buscar info en Mongo por id_checador (si hay conexión)
                nombre = None
                if col_trab is not None:
                    info_trab = col_trab.find_one({"id_checador": uid}, {"nombre": 1})
                    if info_trab and info_trab.get("nombre"):
                        nombre = info_trab["nombre"]

                # Eliminar en checador
                conn.delete_user(uid)

                # Log estilo "agregar"
                if nombre:
                    log_fn(f"🗑️ Eliminado del checador: {nombre}. (ID Checador: {uid})")
                else:
                    log_fn(f"🗑️ Eliminado del checador: (No registrado en Mongo). (ID Checador: {uid})")

                eliminados.append(uid)

            except Exception as e:
                errores += 1
                log_fn(f"❌ Error eliminando ID {uid}: {e}")

        try:
            conn.enable_device(); conn.disconnect()
        except Exception:
            pass

        resumen = {"eliminados": len(eliminados), "candidatos": len(candidatos), "errores": errores}
        log_fn(f"📊 Resumen → {resumen}")
        return resumen

    # ---------------------------
    # Camino con preset_candidatos
    # ---------------------------
    # Con preset, solo hacemos la parte de eliminación (sin repetir detalles)
    conn = None
    checador_ip = None
    zk = None
    for ip in IPS:
        try:
            zk = ZK(ip, port=PUERTO, timeout=8)
            conn = zk.connect()
            checador_ip = ip
            break
        except Exception:
            continue

    if not conn:
        log_fn("❌ No fue posible conectar a ningún checador para aplicar la eliminación.")
        return {"eliminados": [], "candidatos": candidatos, "admins": [], "sede": [], "total_actuales": 0, "errores": 1}

    # Conexión a Mongo para nombres (opcional)
    col_trab = None
    try:
        cliente, msg = conectar_mongo()
        if cliente:
            db = cliente["Registro_Alu"]
            col_trab = db["trabajadores"]
    except Exception:
        col_trab = None

    eliminados = []
    errores = 0

    try:
        conn.disable_device()
        for uid in candidatos:
            try:
                nombre = None
                if col_trab is not None:
                    info_trab = col_trab.find_one({"id_checador": uid}, {"nombre": 1})
                    if info_trab and info_trab.get("nombre"):
                        nombre = info_trab["nombre"]

                conn.delete_user(uid)

                if nombre:
                    log_fn(f"🗑️ Eliminado del checador: {nombre}. (ID Checador: {uid})")
                else:
                    log_fn(f"🗑️ Eliminado del checador: (No registrado en Mongo). (ID Checador: {uid})")

                eliminados.append(uid)
            except Exception as e:
                errores += 1
                log_fn(f"❌ Error eliminando ID {uid}: {e}")
    finally:
        try:
            conn.enable_device(); conn.disconnect()
        except Exception:
            pass

    resumen = {"eliminados": len(eliminados), "candidatos": len(candidatos), "errores": errores}
    log_fn(f"📊 Resumen → {resumen}")
    return resumen
