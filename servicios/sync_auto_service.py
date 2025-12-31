# servicios/sync_auto_service.py
from __future__ import annotations

import os
import csv
import json
import time
import random
import threading
import zipfile
import shutil
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any, List

from dateutil import tz

# ---- Servicios reales (usados cuando simulate=False) ----
from servicios.trabajadores_service import (
    agregar_trabajadores_de_sede,
    eliminar_trabajadores_no_sede,
)
from servicios.asistencias_service import (
    obtener_asistencias,
    limpiar_asistencias_checador,
)


class AutoSyncManager:
    """
    Sincronización automática en segundo plano.

    Flujo real (simulate=False):
      1) agregar_trabajadores_de_sede
      2) eliminar_trabajadores_no_sede (dry-run + eliminación real)
      3) obtener_asistencias (últimos 7 días -> mañana 00:00 local)
      4) limpiar_asistencias_checador (opcional)
      5) respaldos JSON/CSV

    Flujo simulado (simulate=True):
      - Se salta checador/Mongo y genera datos sintéticos coherentes.
      - También guarda respaldos JSON/CSV y un archivo "simulated_raw.json".

    Callbacks:
      - log_fn(str)
      - on_cycle_start()
      - on_cycle_end(ok, result, err)
    """

    # Cambia a False si NO quieres limpiar el checador en cada ciclo (modo real)
    CLEAN_CHECADOR_EACH_CYCLE = True

    # Cuántos días dejamos carpetas sin comprimir antes de pasarlas a .zip
    BACKUP_RETENTION_DAYS = 30

    def __init__(
        self,
        sede_id: int,
        checador_ip: str,
        interval_min: int = 10,
        retries: int = 3,
        log_fn: Optional[Callable[[str], None]] = None,
        on_cycle_start: Optional[Callable[[], None]] = None,
        on_cycle_end: Optional[Callable[[bool, Optional[dict], Optional[Exception]], None]] = None,
        *,
        simulate: bool = False,
        sim_opts: Optional[Dict[str, Any]] = None,
        backup_root: Optional[str] = None,
    ):
        self.sede_id = int(sede_id) if sede_id is not None else 0
        self.checador_ip = checador_ip or ""
        self.interval_min = max(1, int(interval_min))
        self.retries = max(1, int(retries))
        self.log_fn = log_fn or (lambda msg: None)
        self.on_cycle_start = on_cycle_start
        self.on_cycle_end = on_cycle_end

        self.simulate = bool(simulate)
        self.sim_opts = sim_opts or {}
        # opciones de simulación por defecto
        self._sim_defaults = {
            "min_workers": 8,
            "max_workers": 18,
            "days_back": 7,
            "late_probability": 0.18,    # prob de llegada tarde
            "miss_probability": 0.07,    # prob de falta
            "halfday_probability": 0.06, # prob de media jornada
            "seed": None,                # si se define, hace la simulación reproducible
        }

        self._tz_local = tz.gettz("America/Mexico_City")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run_utc: Optional[datetime] = None

        # rutas de respaldo / logs
        os.makedirs("logs", exist_ok=True)
        # backup_root = carpeta base para auto-sync (la recibe menu.py)
        self.backup_root = backup_root or os.path.join("respaldos", "automaticos")
        os.makedirs(self.backup_root, exist_ok=True)

        self._log_path = os.path.join("logs", "auto_sync.log")

    # ---------- API pública ----------
    def start(self) -> bool:
        if self.is_running:
            return False
        if not self.sede_id:
            self._log_ui("⚠️ Auto-Sync: falta 'sede_id'. No puedo iniciar.")
            return False
        if not self.checador_ip and not self.simulate:
            self._log_ui("⚠️ Auto-Sync: falta 'checador_ip' (requerido en modo real).")
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._log_file(f"🟢 Auto-Sync iniciado (simulate={self.simulate})")
        self._log_ui(f"🟢 Auto-Sync iniciado (simulate={self.simulate})")
        return True

    def stop(self) -> bool:
        if not self.is_running:
            return False
        self._stop.set()
        try:
            self._thread.join(timeout=5)
        except Exception:
            pass
        self._thread = None
        self._log_file("🔴 Auto-Sync detenido")
        self._log_ui("🔴 Auto-Sync detenido")
        return True

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_interval(self, minutes: int) -> None:
        self.interval_min = max(1, int(minutes))
        self._log_file(f"⏱️ Intervalo actualizado a {self.interval_min} min.")
        self._log_ui(f"⏱️ Intervalo actualizado a {self.interval_min} min.")

    @property
    def last_run_utc(self) -> Optional[datetime]:
        return self._last_run_utc

    # ---------- Bucle principal ----------
    def _loop(self):
        while not self._stop.is_set():
            self._one_cycle()
            # Dormir por pasos cortos para responder rápido a stop()
            remaining = self.interval_min * 60
            step = 0.5
            while remaining > 0 and not self._stop.is_set():
                time.sleep(step)
                remaining -= step

    def _one_cycle(self):
        """Ejecuta una corrida completa (real o simulación), con respaldos y callbacks."""
        if self.on_cycle_start:
            try:
                self.on_cycle_start()
            except Exception:
                pass

        self._log_ui("▶️ Auto-Sync: iniciando corrida…")
        self._log_file("▶️ Iniciando corrida…")

        ok = False
        result: Optional[dict] = None
        err: Optional[Exception] = None

        # Ventana de tiempo local: últimos 7 días 00:00 .. mañana 00:00 (exclusivo)
        ahora_local = datetime.now(self._tz_local)
        desde_local = (ahora_local - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        manana_local = (ahora_local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        try:
            if self.simulate:
                # --------- MODO SIMULACIÓN ---------
                self._log_ui("🧪 Simulación activada (no se usa checador/Mongo).")
                result = self._simulate_cycle(desde_local, manana_local)
                ok = True
            else:
                # --------- MODO REAL ---------
                # 1) Agregar trabajadores
                try:
                    self._log_ui("👥 Agregando trabajadores de la sede al checador…")
                    agregar_trabajadores_de_sede(sede_id=self.sede_id, log_fn=self._log_ui)
                except Exception as e:
                    self._log_ui(f"⚠️ agregar_trabajadores_de_sede: {e}")
                    self._log_file(f"⚠️ agregar_trabajadores_de_sede: {e}")

                # 2) Eliminar no-sede
                try:
                    self._log_ui("🧹 Calculando candidatos a eliminar (usuarios no-sede)…")
                    preview = eliminar_trabajadores_no_sede(
                        sede_id=self.sede_id,
                        log_fn=self._log_ui,
                        dry_run=True,
                        show_details=True,
                        preset_candidatos=None
                    )
                    candidatos = (preview or {}).get("candidatos", [])
                    if candidatos:
                        self._log_ui(f"🗑️ Eliminando definitivamente {len(candidatos)} ID(s)…")
                        eliminar_trabajadores_no_sede(
                            sede_id=self.sede_id,
                            log_fn=self._log_ui,
                            dry_run=False,
                            show_details=False,
                            preset_candidatos=candidatos
                        )
                    else:
                        self._log_ui("✨ No hay nada que eliminar. Checador limpio.")
                except Exception as e:
                    self._log_ui(f"⚠️ eliminar_trabajadores_no_sede: {e}")
                    self._log_file(f"⚠️ eliminar_trabajadores_no_sede: {e}")

                # 3) Obtener asistencias (con reintentos)
                intento = 0
                while intento < self.retries and not self._stop.is_set():
                    intento += 1
                    try:
                        self._log_ui(f"⏳ Obteniendo asistencias (intento {intento}/{self.retries})…")
                        result = obtener_asistencias(
                            sede_id=self.sede_id,
                            checador_ip=self.checador_ip,
                            desde=desde_local,
                            hasta=manana_local,  # tope EXCLUSIVO
                            log_fn=lambda m: self._log_ui("  " + m),
                        )
                        ok = bool(result and result.get("ok"))
                        if ok:
                            break
                    except Exception as e:
                        err = e
                        self._log_ui(f"⚠️ Error en obtener_asistencias intento {intento}: {e}")
                        self._log_file(f"⚠️ obtener_asistencias intento {intento}: {e}")
                        time.sleep(2)

                # 4) Limpiar checador (opcional)
                if self.CLEAN_CHECADOR_EACH_CYCLE:
                    try:
                        self._log_ui("🗑️ Limpiando asistencias antiguas en el checador…")
                        limpiar_asistencias_checador(checador_ip=self.checador_ip, log_fn=self._log_ui)
                    except Exception as e:
                        self._log_ui(f"⚠️ limpiar_asistencias_checador: {e}")
                        self._log_file(f"⚠️ limpiar_asistencias_checador: {e}")

            self._last_run_utc = datetime.utcnow()

            # Respaldos (comunes a real y simulado)
            try:
                self._write_backups(
                    ok=ok,
                    result=result,
                    desde_local=desde_local,
                    hasta_local=manana_local,
                )
            except Exception as e:
                self._log_ui(f"⚠️ Error guardando respaldos: {e}")
                self._log_file(f"⚠️ Error guardando respaldos: {e}")

            # Resumen final
            if ok:
                ins = int((result or {}).get("insertados", 0))
                upd = int((result or {}).get("actualizados", 0))
                tot = int((result or {}).get("eventos", 0))
                msg = f"✅ Corrida completada. Insertados: {ins} | Actualizados: {upd} | Eventos: {tot}"
                self._log_ui(msg)
                self._log_file(msg)

                # Mostrar rutas de respaldos si vienen desde obtener_asistencias
                respaldos = (result or {}).get("respaldos")
                if respaldos:
                    try:
                        rutas = []
                        if isinstance(respaldos, dict):
                            for v in respaldos.values():
                                if isinstance(v, (list, tuple)):
                                    rutas.extend([str(x) for x in v])
                                else:
                                    rutas.append(str(v))
                        elif isinstance(respaldos, (list, tuple)):
                            rutas = [str(x) for x in respaldos]
                        if rutas:
                            msg_r = "📦 Respaldos guardados: " + " | ".join(rutas)
                            self._log_ui(msg_r)
                            self._log_file(msg_r)
                    except Exception:
                        pass
            else:
                msg = f"❌ Corrida fallida tras {self.retries} intento(s)."
                self._log_ui(msg)
                self._log_file(msg)

        except Exception as e:
            err = e
            self._log_ui(f"💥 Error no controlado en corrida: {e}")
            self._log_file(f"💥 Error no controlado en corrida: {e}")

        finally:
            if self.on_cycle_end:
                try:
                    self.on_cycle_end(ok, result if ok else None, err)
                except Exception:
                    pass

    # ---------- Simulación ----------
    def _simulate_cycle(self, desde_local: datetime, hasta_local: datetime) -> dict:
        """
        Genera un conjunto de 'asistencias' sintéticas para N trabajadores,
        con probabilidades de llegada tarde, falta y media jornada.

        Devuelve un dict con la misma estructura base que 'obtener_asistencias' devuelve:
          { ok: True, insertados: int, actualizados: int, eventos: int }
        y además escribe un archivo 'simulated_raw.json' con el detalle generado.
        """
        cfg = {**self._sim_defaults, **self.sim_opts}
        if cfg["seed"] is None:
            # seed suave para variación por sede + hora
            seed = int(time.time()) ^ (self.sede_id << 8)
        else:
            seed = int(cfg["seed"])
        rnd = random.Random(seed)

        # número de trabajadores simulados
        n_workers = rnd.randint(cfg["min_workers"], cfg["max_workers"])

        # días (desde_local inclusive hasta 'hasta_local' exclusivo)
        dias: List[datetime] = []
        d = desde_local
        while d < hasta_local:
            dias.append(d)
            d += timedelta(days=1)

        raw: List[dict] = []
        insertados = 0
        actualizados = 0
        total_eventos = 0

        for w in range(n_workers):
            worker_id = 1000 + w  # IDs sintéticos
            for dia in dias:
                estado, detalle = self._simulate_day(rnd, dia, cfg)
                total_eventos += len(detalle)
                if estado == "Asistencia Completa":
                    insertados += 1  # métrica simbólica
                elif estado in ("Entrada sin salida", "Media Jornada"):
                    actualizados += 1

                raw.append({
                    "trabajador": worker_id,
                    "sede": self.sede_id,
                    "fecha": dia.date().isoformat(),
                    "estado": estado,
                    "detalle": detalle,
                })

        # Guardar detalle de simulación (además del backup general)
        self._write_simulated_raw(raw)

        return {
            "ok": True,
            "insertados": insertados,
            "actualizados": actualizados,
            "eventos": total_eventos,
            "simulated": True,
            "workers": n_workers,
            "days": len(dias),
        }

    def _simulate_day(self, rnd: random.Random, dia: datetime, cfg: Dict[str, Any]):
        """
        Devuelve (estado, detalle[]) para un día simulado.
        """
        # probabilidades
        p_late = float(cfg["late_probability"])
        p_miss = float(cfg["miss_probability"])
        p_half = float(cfg["halfday_probability"])

        # orden: falta > media jornada > tarde > normal
        if rnd.random() < p_miss:
            # Falta: sin detalle
            return "Falta", []

        base_entrada = dia.replace(hour=8, minute=0, second=0, microsecond=0)
        base_salida = dia.replace(hour=16, minute=0, second=0, microsecond=0)

        if rnd.random() < p_half:
            # Media jornada (entrada fija, salida antes)
            salida = base_entrada + timedelta(hours=4, minutes=rnd.randint(-10, 10))
            return "Media Jornada", [
                {"tipo": "Entrada", "fechaHora": base_entrada.isoformat()},
                {"tipo": "Salida", "fechaHora": salida.isoformat()},
            ]

        # tarde?
        entrada = base_entrada + timedelta(minutes=rnd.randint(6, 45)) if rnd.random() < p_late else base_entrada
        # 10% sin salida (entrada sin salida)
        if rnd.random() < 0.10:
            return "Entrada sin salida", [
                {"tipo": "Entrada", "fechaHora": entrada.isoformat()},
            ]

        # normal completa (con ligera variación en salida)
        salida = base_salida + timedelta(minutes=rnd.randint(-15, 20))
        return "Asistencia Completa", [
            {"tipo": "Entrada", "fechaHora": entrada.isoformat()},
            {"tipo": "Salida", "fechaHora": salida.isoformat()},
        ]

    def _write_simulated_raw(self, rows: List[dict]) -> None:
        """
        Guarda el detalle crudo de la simulación del ciclo actual en:
          backup_root/YYYY-MM-DD/simulated_raw_YYYYMMDD_HHMMSS.json
        """
        fecha_dir = datetime.now(self._tz_local).strftime("%Y-%m-%d")
        dirpath = os.path.join(self.backup_root, fecha_dir)
        os.makedirs(dirpath, exist_ok=True)
        ts_name = datetime.now(self._tz_local).strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(dirpath, f"simulated_raw_{ts_name}.json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- Respaldos ----------
    def _write_backups(self, ok: bool, result: Optional[dict],
                       desde_local: datetime, hasta_local: datetime) -> None:
        """
        Guarda un JSON y un CSV de resumen en:
            backup_root/YYYY-MM-DD/auto_sync_YYYYMMDD_HHMMSS.json
            backup_root/YYYY-MM-DD/auto_sync_summary.csv  (append)

        Y además comprime en .zip las carpetas de días más antiguos que BACKUP_RETENTION_DAYS.
        """
        fecha_dir = datetime.now(self._tz_local).strftime("%Y-%m-%d")
        dirpath = os.path.join(self.backup_root, fecha_dir)
        os.makedirs(dirpath, exist_ok=True)

        ts_name = datetime.now(self._tz_local).strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(dirpath, f"auto_sync_{ts_name}.json")
        csv_path = os.path.join(dirpath, "auto_sync_summary.csv")

        resumen = {
            "timestamp_local": datetime.now(self._tz_local).isoformat(),
            "sede_id": self.sede_id,
            "checador_ip": self.checador_ip,
            "simulate": self.simulate,
            "ventana_desde": desde_local.isoformat(),
            "ventana_hasta_exclusivo": hasta_local.isoformat(),
            "ok": ok,
            "result": result or {},
        }
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(resumen, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # no romper la corrida por IO

        ins = int((result or {}).get("insertados", 0))
        upd = int((result or {}).get("actualizados", 0))
        tot = int((result or {}).get("eventos", 0))
        row = [
            resumen["timestamp_local"],
            self.sede_id,
            self.checador_ip,
            "SIM" if self.simulate else "REAL",
            desde_local.strftime("%Y-%m-%d"),
            hasta_local.strftime("%Y-%m-%d"),
            "OK" if ok else "FAIL",
            ins, upd, tot,
        ]
        write_header = not os.path.exists(csv_path)
        try:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow([
                        "timestamp_local",
                        "sede_id",
                        "checador_ip",
                        "modo",
                        "desde_dia",
                        "hasta_dia_excl",
                        "status",
                        "insertados",
                        "actualizados",
                        "eventos",
                    ])
                w.writerow(row)
        except Exception:
            pass

        # Comprimir carpetas viejas
        self._compress_old_backups()

    def _compress_old_backups(self, retention_days: int = None) -> None:
        """
        Comprime carpetas de días viejos en backup_root:

        backup_root/
          ├─ 2025-11-01/  ->  2025-11-01.zip (y se borra la carpeta)
          ├─ 2025-11-02/
          └─ ...

        retention_days: cuántos días se mantienen sin comprimir.
        """
        if retention_days is None:
            retention_days = self.BACKUP_RETENTION_DAYS

        try:
            if not os.path.isdir(self.backup_root):
                return

            hoy = datetime.now(self._tz_local).date()
            umbral = hoy - timedelta(days=retention_days)

            for nombre in os.listdir(self.backup_root):
                ruta_carpeta = os.path.join(self.backup_root, nombre)
                if not os.path.isdir(ruta_carpeta):
                    continue

                # nombre esperado: YYYY-MM-DD
                try:
                    fecha_carpeta = datetime.strptime(nombre, "%Y-%m-%d").date()
                except ValueError:
                    # no es carpeta de fecha
                    continue

                # Sólo comprimimos si la fecha es <= umbral
                if fecha_carpeta > umbral:
                    continue

                zip_path = os.path.join(self.backup_root, f"{nombre}.zip")
                if os.path.exists(zip_path):
                    # Ya existe el zip; podemos eliminar la carpeta si sigue ahí
                    try:
                        shutil.rmtree(ruta_carpeta)
                    except Exception:
                        pass
                    continue

                try:
                    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                        for root, dirs, files in os.walk(ruta_carpeta):
                            for file in files:
                                full_path = os.path.join(root, file)
                                rel_path = os.path.relpath(full_path, ruta_carpeta)
                                # guardamos con prefijo de carpeta de fecha para mantener contexto
                                arcname = os.path.join(nombre, rel_path)
                                zf.write(full_path, arcname=arcname)
                    shutil.rmtree(ruta_carpeta)
                    self._log_file(f"📦 Carpeta de respaldos comprimida: {zip_path}")
                except Exception as e:
                    self._log_file(f"⚠️ No se pudo comprimir {ruta_carpeta}: {e}")
        except Exception as e:
            self._log_file(f"⚠️ Error general al comprimir respaldos antiguos: {e}")

    # ---------- Logging ----------
    def _log_file(self, msg: str):
        ts = datetime.now(self._tz_local).strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def _log_ui(self, msg: str):
        try:
            self.log_fn(msg)
        except Exception:
            pass
