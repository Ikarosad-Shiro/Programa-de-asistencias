# servicios/trabajadores_service.py
import json
import os
import socket
from datetime import datetime
from pymongo import UpdateOne
from pymongo.errors import PyMongoError
from zk import ZK
from typing import Set, Iterable, Dict, Any

from servicios.mongo_service import conectar_mongo
from servicios.checador_service import IPS, PUERTO  # usamos tu listado de IPs y puerto

# 🔒 1..100 reservado (admins/servicio/etc.)
RANGO_RESERVADO_MAX = 99  # trabajadores válidos: id_checador > 100

def _uid_from_zk_user(u) -> int | None:
    """
    Extrae UID/int del usuario del checador (ZKTeco), tolerante a dicts/objetos.
    """
    if isinstance(u, dict):
        for k in ("user_id", "uid", "uid_number", "uidNumber", "enroll_id", "id"):
            v = u.get(k)
            if v is not None:
                try:
                    return int(str(v).strip())
                except Exception:
                    pass
        return None

    for attr in ("user_id", "uid", "uid_number", "uidNumber", "enroll_id", "id"):
        if hasattr(u, attr):
            try:
                return int(str(getattr(u, attr)).strip())
            except Exception:
                pass
    return None

def _to_int_list(xs: Iterable[Any]) -> list[int]:
    out = []
    for x in (xs or []):
        try:
            out.append(int(x))
        except Exception:
            continue
    return out

def _build_or_merge_sync_list(trab: dict, sedes_map: Dict[int, str]) -> list[dict]:
    """
    Devuelve el arreglo sincronizacionSedes completo (principal + foráneas),
    preservando estados existentes y agregando faltantes con sincronizado=False.
    Estructura de cada item:
      { "id": <int>, "nombre": <str>, "sincronizado": <bool>, "fecha": <ISO> }
    """
    ids = []
    sedeP = trab.get("sedePrincipal", trab.get("sede"))
    if isinstance(sedeP, int) or (isinstance(sedeP, str) and sedeP.isdigit()):
        ids.append(int(sedeP))
    ids.extend(_to_int_list(trab.get("sedesForaneas")))

    ids = sorted(set(ids))
    existing = trab.get("sincronizacionSedes") or []
    by_id = {e.get("id"): e for e in existing if isinstance(e, dict) and "id" in e}

    merged = []
    for sid in ids:
        e = by_id.get(sid, {"id": sid, "nombre": sedes_map.get(sid, f"Sede {sid}"), "sincronizado": False})
        if "nombre" not in e:
            e["nombre"] = sedes_map.get(sid, f"Sede {sid}")
        if "sincronizado" not in e:
            e["sincronizado"] = False
        merged.append(e)

    return merged

def _all_true_sync(sync_list: list[dict], expected_ids: set[int]) -> bool:
    """
    True si todos los ids esperados existen en el arreglo y están sincronizado=True.
    """
    seen = {e.get("id") for e in (sync_list or []) if e.get("sincronizado") is True}
    return expected_ids and expected_ids.issubset(seen)

def _get_privilege(u):
    """
    Intenta leer el privilegio del usuario del checador.
    Soporta dicts y objetos. Devuelve un int si puede, o un str normalizado.
    Convenciones comunes ZK: 0=USER, 1..=ADMIN/MANAGER (a veces 14/15).
    """
    val = None
    keys = ("privilege", "role", "privilege_id", "user_privilege")
    if isinstance(u, dict):
        for k in keys:
            if k in u and u[k] is not None:
                val = u[k]
                break
    else:
        for k in keys:
            if hasattr(u, k):
                val = getattr(u, k)
                break

    if val is None:
        return None

    # normaliza
    try:
        return int(str(val).strip())
    except Exception:
        s = str(val).strip().lower()
        # casos tipo 'admin', 'manager', 'user'
        if "admin" in s or "manager" in s or "super" in s:
            return 1  # lo tratamos como admin
        if "user" in s or "normal" in s:
            return 0
        return None

def _is_device_admin(u) -> bool:
    """
    True si el usuario del checador es admin/manager según privilege.
    """
    p = _get_privilege(u)
    if p is None:
        return False
    # criterio conservador: cualquier valor > 0 lo tratamos como admin
    return isinstance(p, int) and p > 0


#-------------------------------------------

def _cargar_config():
    if not os.path.exists("configuracion_temporal.json"):
        return None
    with open("configuracion_temporal.json", "r", encoding="utf-8") as f:
        return json.load(f)

def agregar_trabajadores_de_sede(sede_id: int, log_fn=print):
    """
    Agrega/actualiza en el checador TODOS los trabajadores activos cuya sedePrincipal == sede_id
    o que tengan sede_id en sus sedesForaneas. Además:
      - Crea/Merge el arreglo `sincronizacionSedes` con todas sus sedes (principal + foráneas).
      - Marca como True la entrada de la sede actual al agregarlos al checador (si no existían).
      - Actualiza `sincronizado` del trabajador a True solo si TODAS sus sedes están True.
    """
    try:
        # Conexión a Mongo
        from servicios.mongo_service import conectar_mongo
        cliente, msg = conectar_mongo()
        if not cliente:
            log_fn(f"❌ Mongo: {msg}")
            return
        db = cliente["Registro_Alu"]

        # Mapa de sedes (id -> nombre)
        sedes_docs = list(db.sedes.find({}, {"_id": 0, "id": 1, "nombre": 1}))
        sedes_map = {int(d["id"]): d.get("nombre", f"Sede {d['id']}") for d in sedes_docs if "id" in d}

        # Conexión al checador
        from servicios.checador_service import conectar_checador
        conn, ip = conectar_checador()
        if not conn:
            log_fn("❌ No se pudo conectar al checador.")
            return
        log_fn(f"🔌 Conectado al checador en {ip}")

        # Usuarios existentes en el checador (para evitar duplicados)
        try:
            users = conn.get_users()
            existentes = set()
            for u in users:
                n = _uid_from_zk_user(u)
                if n is not None:
                    existentes.add(n)
        except Exception as e:
            log_fn(f"⚠️ No se pudieron listar usuarios del checador: {e}")
            existentes = set()

        # Query de trabajadores objetivo (activos con id_checador)
        q = {
            "estado": "activo",
            "id_checador": {"$ne": None},
            "$or": [
                {"sedePrincipal": sede_id},
                {"sedesForaneas": sede_id},
                {"sede": sede_id}  # respaldo por si aún usan `sede`
            ]
        }
        trabajadores = list(db.trabajadores.find(q))

        if not trabajadores:
            log_fn("ℹ️ No hay trabajadores que coincidan con esta sede.")
            try:
                conn.disconnect()
            except Exception:
                pass
            return

        log_fn(f"👷‍♀️ Trabajadores candidatos: {len(trabajadores)}")

        procesados, dados_de_alta, ya_existian, errores = 0, 0, 0, 0

        for t in trabajadores:
            procesados += 1
            nombre = t.get("nombre", "(sin nombre)")
            try:
                uid = int(str(t["id_checador"]))
            except Exception:
                log_fn(f"⛔ {nombre}: id_checador inválido → {t.get('id_checador')}")
                errores += 1
                continue

            # 1) Construir/merge sincronizacionSedes (todas las sedes del trabajador)
            ids_sedes = []
            sedeP = t.get("sedePrincipal", t.get("sede"))
            if isinstance(sedeP, int) or (isinstance(sedeP, str) and sedeP.isdigit()):
                ids_sedes.append(int(sedeP))
            ids_sedes.extend(_to_int_list(t.get("sedesForaneas")))
            ids_sedes = sorted(set(ids_sedes))
            expected_set = set(ids_sedes)

            sync_list = _build_or_merge_sync_list(t, sedes_map)
            # Persistir merge si cambió la forma
            db.trabajadores.update_one(
                {"_id": t["_id"]},
                {"$set": {"sincronizacionSedes": sync_list}}
            )

            # 2) ¿Debe estar en ESTE checador (sede actual)?
            if sede_id not in expected_set:
                # No corresponde a esta sede; no lo toques en checador, pero deja su lista completa
                log_fn(f"↪️ {nombre}: no pertenece a esta sede ({sede_id}); skip en checador.")
            else:
                # 3) Alta en checador si no existe
                try:
                    if uid not in existentes:
                        # set_user: cuida los parámetros de tu SDK/ZK
                        conn.set_user(uid=uid, name=nombre, privilege=0, password="")
                        existentes.add(uid)
                        dados_de_alta += 1
                        log_fn(f"✅ Alta checador: {uid} - {nombre}")
                    else:
                        ya_existian += 1
                        log_fn(f"↔️ Ya estaba en checador: {uid} - {nombre}")

                    # 4) Marcar sede actual como sincronizada en el arreglo
                    db.trabajadores.update_one(
                        {"_id": t["_id"], "sincronizacionSedes.id": sede_id},
                        {"$set": {
                            "sincronizacionSedes.$.sincronizado": True,
                            "sincronizacionSedes.$.fecha": datetime.utcnow().isoformat()
                        }}
                    )
                except Exception as e:
                    errores += 1
                    log_fn(f"❌ Error alta checador {uid} - {nombre}: {e}")
                    continue

            # 5) Releer lista y actualizar bandera global `sincronizado`
            t_ref = db.trabajadores.find_one({"_id": t["_id"]}, {"sincronizacionSedes": 1})
            all_true = _all_true_sync(t_ref.get("sincronizacionSedes", []), expected_set)
            db.trabajadores.update_one({"_id": t["_id"]}, {"$set": {"sincronizado": bool(all_true)}})

        # Resumen
        log_fn(f"— Fin — Procesados: {procesados} | Altas: {dados_de_alta} | Ya estaban: {ya_existian} | Errores: {errores}")

        try:
            conn.disconnect()
        except Exception:
            pass

    except Exception as e:
        log_fn(f"🧨 Error inesperado en agregar_trabajadores_de_sede: {e}")

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
    sede_id: int,
    log_fn=print,
    dry_run: bool = True,
    show_details: bool = True,
    preset_candidatos: list[dict] | None = None
):
    """
    Elimina del checador a usuarios que NO pertenecen a la sede actual.
    Protecciones:
      - NO eliminar admins del checador (privilege > 0).
      - NO eliminar trabajadores con rol admin en Mongo.
      - Inactivos sí son candidatos a eliminar.
    También limpia 'sincronizacionSedes' y recalcula 'sincronizado'.
    """
    try:
        # 1) Mongo
        from servicios.mongo_service import conectar_mongo
        cliente, msg = conectar_mongo()
        if not cliente:
            log_fn(f"❌ Mongo: {msg}")
            return {"error": msg}
        db = cliente["Registro_Alu"]

        sedes_docs = list(db.sedes.find({}, {"_id": 0, "id": 1, "nombre": 1}))
        sedes_map = {int(d["id"]): d.get("nombre", f"Sede {d['id']}") for d in sedes_docs if "id" in d}

        # 2) Checador
        from servicios.checador_service import conectar_checador
        conn, ip = conectar_checador()
        if not conn:
            log_fn("❌ No se pudo conectar al checador.")
            return {"error": "no_checador"}
        log_fn(f"🔌 Conectado al checador en {ip}")

        # 3) Usuarios en checador: guardamos UID y si es admin en dispositivo
        users = []
        try:
            users = conn.get_users()
        except Exception as e:
            log_fn(f"⚠️ No se pudieron listar usuarios del checador: {e}")

        presentes_info: dict[int, bool] = {}
        for u in users:
            uid = _uid_from_zk_user(u)
            if uid is None:
                continue
            presentes_info[uid] = _is_device_admin(u)

        presentes_set = set(presentes_info.keys())

        # 4) Trabajadores en Mongo (map por id_checador)
        ROLES_EXCLUIDOS = {"admin", "administrador", "dios", "superadmin"}
        cand_docs = list(db.trabajadores.find({"id_checador": {"$ne": None}}))
        por_id = {}
        for t in cand_docs:
            try:
                uid = int(str(t.get("id_checador")))
            except Exception:
                continue
            por_id[uid] = t

        def expected_set_for(t: dict) -> set[int]:
            ids = []
            sp = t.get("sedePrincipal", t.get("sede"))
            if isinstance(sp, int) or (isinstance(sp, str) and sp.isdigit()):
                ids.append(int(sp))
            ids.extend(_to_int_list(t.get("sedesForaneas")))
            return set(ids)

        # 5) Detección de candidatos
        candidatos = []
        admins_device = []     # admins detectados en el equipo (protegidos)
        admins_mongo = []      # admins por rol en Mongo (protegidos)
        inactivos = []
        desconocidos = []

        for uid, is_admin_dev in presentes_info.items():
            if is_admin_dev:
                admins_device.append(uid)
                continue  # 👈 NUNCA se elimina admin de dispositivo

            t = por_id.get(uid)
            if not t:
                desconocidos.append(uid)
                # Desconocido (no está en Mongo) y no-admin en dispositivo -> candidato
                candidatos.append({
                    "uid": uid,
                    "nombre": f"(desconocido:{uid})",
                    "motivo": "sin_registro_en_mongo",
                    "sedePrincipal": None,
                    "sedesForaneas": [],
                    "permitidas": []
                })
                continue

            estado = str(t.get("estado", "")).lower()
            rol = str(t.get("rol", "")).lower()
            if rol in ROLES_EXCLUIDOS:
                admins_mongo.append({"uid": uid, "nombre": t.get("nombre", "")})
                continue  # 👈 NUNCA se elimina admin de Mongo

            if estado != "activo":
                inactivos.append({"uid": uid, "nombre": t.get("nombre", "")})
                # inactivo -> candidato a eliminar
                candidatos.append({
                    "uid": uid,
                    "nombre": t.get("nombre", ""),
                    "motivo": "inactivo",
                    "sedePrincipal": t.get("sedePrincipal", t.get("sede")),
                    "sedesForaneas": _to_int_list(t.get("sedesForaneas")),
                    "permitidas": list(expected_set_for(t))
                })
                continue

            permitidas = expected_set_for(t)
            if sede_id not in permitidas:
                candidatos.append({
                    "uid": uid,
                    "nombre": t.get("nombre", ""),
                    "motivo": "no_pertenece_a_esta_sede",
                    "sedePrincipal": t.get("sedePrincipal", t.get("sede")),
                    "sedesForaneas": _to_int_list(t.get("sedesForaneas")),
                    "permitidas": list(permitidas)
                })

        # Preview
        if dry_run and show_details:
            log_fn(f"👥 Usuarios en checador: {len(presentes_set)}")
            log_fn(f"🟣 Admins en dispositivo (protegidos): {len(admins_device)} → {sorted(admins_device)[:20]}")
            log_fn(f"🔵 Admins en Mongo (protegidos): {len(admins_mongo)}")
            log_fn(f"🟡 Desconocidos: {len(desconocidos)}")
            log_fn(f"🟠 Inactivos: {len(inactivos)}")
            log_fn(f"🧹 Candidatos a eliminar (sede {sede_id}): {len(candidatos)}")
            for c in candidatos[:50]:
                log_fn(f"   - {c['uid']} · {c['nombre']} · motivo={c['motivo']} · permitidas={c['permitidas']}")

        aplicar_sobre = candidatos if preset_candidatos is None else preset_candidatos

        borrados, errores = 0, 0
        if not dry_run:
            for c in aplicar_sobre:
                uid = c.get("uid")
                nombre = c.get("nombre", "")

                # Por seguridad extra: si justo antes de borrar alguien cambiara a admin en el dispositivo, revalida:
                try:
                    # algunos SDKs permiten get_user(uid); si no, recarga users list
                    # fallback: si estaba marcado como admin_device, skip
                    if uid in admins_device:
                        log_fn(f"⛔ Saltado (admin en dispositivo): {uid} - {nombre}")
                        continue
                except Exception:
                    pass

                try:
                    try:
                        conn.delete_user(uid)
                    except TypeError:
                        conn.delete_user(uid=uid)
                    borrados += 1
                    log_fn(f"🗑️ Eliminado del checador: {uid} - {nombre}")
                except Exception as e:
                    errores += 1
                    log_fn(f"❌ Error al eliminar {uid} - {nombre}: {e}")
                    continue

                # Actualizaciones en Mongo (si existe)
                t = por_id.get(uid)
                if not t:
                    continue

                # Reconstituye/mergea la lista de sincronización para mantener solo sedes válidas
                sync_list = _build_or_merge_sync_list(t, sedes_map)
                db.trabajadores.update_one(
                    {"_id": t["_id"]},
                    {"$set": {"sincronizacionSedes": sync_list}}
                )

                # Recalcula bandera global
                permitidas = expected_set_for(t)
                t_ref = db.trabajadores.find_one({"_id": t["_id"]}, {"sincronizacionSedes": 1})
                all_true = _all_true_sync(t_ref.get("sincronizacionSedes", []), permitidas)
                db.trabajadores.update_one({"_id": t["_id"]}, {"$set": {"sincronizado": bool(all_true)}})

        try:
            conn.disconnect()
        except Exception:
            pass

        return {
            "resumen": {
                "presentes": len(presentes_set),
                "admins_device": len(admins_device),
                "admins_mongo": len(admins_mongo),
                "desconocidos": len(desconocidos),
                "inactivos": len(inactivos),
                "candidatos": len(candidatos),
                "borrados": borrados,
                "errores": errores,
                "sede": sede_id,
                "dry_run": dry_run,
            },
            "candidatos": candidatos if dry_run else aplicar_sobre
        }

    except Exception as e:
        log_fn(f"🧨 Error en eliminar_trabajadores_no_sede: {e}")
        return {"error": str(e)}
