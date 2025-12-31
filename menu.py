# -*- coding: utf-8 -*-
"""
menu.py — Servidor Local Disboart / Alu Asistencias

• UI moderna con ttkbootstrap.
• Modos: Sincronización Manual y Automática.
• Barra de progreso no bloqueante.
• Logs en pantalla + guardado a archivo.
• Respaldos locales:
    - respaldos/automaticos/
    - respaldos/manuales/
  En modo MANUAL, el respaldo se genera con la estructura:
    {
      "sede": <int>,
      "checador_ip": "<ip>",
      "generado": "<ISO local>",
      "documentos": [ {trabajador, sede, fecha, detalle: [...]}, ... ]
    }
"""

import os
import sys
import json
import time
import queue
import threading
from datetime import datetime, timedelta

import shutil
import zipfile

# Tk / ttkbootstrap
import tkinter as tk
from tkinter import messagebox, filedialog
import ttkbootstrap as tb
from ttkbootstrap.constants import *

# Zona horaria local
from tzlocal import get_localzone
TZ_LOCAL = get_localzone()

# ==============================
# Rutas y configuración
# ==============================
APP_TITLE = "Disboart – Alu Asistencias (Servidor Local)"
CFG_PATH = "configuracion_temporal.json"

LOG_DIR = "logs"
BACKUP_DIR = "respaldos"
BACKUP_AUTO_DIR = os.path.join(BACKUP_DIR, "automaticos")
BACKUP_MANUAL_DIR = os.path.join(BACKUP_DIR, "manuales")

# Días que se mantienen carpetas sin comprimir
BACKUP_RETENTION_DAYS = 30

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(BACKUP_AUTO_DIR, exist_ok=True)
os.makedirs(BACKUP_MANUAL_DIR, exist_ok=True)


def _safe_ip(ip: str) -> str:
    return ip.replace(".", "-") if ip else "unknown"


def cargar_configuracion() -> dict:
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_configuracion(cfg: dict) -> None:
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[cfg] No se pudo guardar la configuración: {e}")


def _compress_old_backup_folders(base_dir: str, retention_days: int = BACKUP_RETENTION_DAYS):
    """
    Comprime carpetas de días viejos dentro de base_dir.

    base_dir/
      ├─ 2025-11-01/  ->  2025-11-01.zip (y se borra la carpeta)
      ├─ 2025-11-02/
      └─ ...

    retention_days: cuántos días se mantienen sin comprimir.
    """
    try:
        if not os.path.isdir(base_dir):
            return

        hoy = datetime.now(TZ_LOCAL).date()
        umbral = hoy - timedelta(days=retention_days)

        for nombre in os.listdir(base_dir):
            ruta_carpeta = os.path.join(base_dir, nombre)
            if not os.path.isdir(ruta_carpeta):
                continue

            # nombre esperado: YYYY-MM-DD
            try:
                fecha_carpeta = datetime.strptime(nombre, "%Y-%m-%d").date()
            except ValueError:
                # no es carpeta de fecha
                continue

            if fecha_carpeta > umbral:
                continue

            zip_path = os.path.join(base_dir, f"{nombre}.zip")
            if os.path.exists(zip_path):
                # Si ya existe el zip, podemos borrar la carpeta
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
                            arcname = os.path.join(nombre, rel_path)
                            zf.write(full_path, arcname=arcname)
                shutil.rmtree(ruta_carpeta)
            except Exception as e:
                print(f"[backup] No se pudo comprimir {ruta_carpeta}: {e}")
    except Exception as e:
        print(f"[backup] Error general al comprimir respaldos antiguos en {base_dir}: {e}")


# ==============================
# Importar servicios reales
# ==============================
from servicios.trabajadores_service import (
    agregar_trabajadores_de_sede,
    eliminar_trabajadores_no_sede,
)
from servicios.asistencias_service import (
    obtener_asistencias,
    limpiar_asistencias_checador,
)
from servicios.checador_service import detectar_checador
from servicios.sync_auto_service import AutoSyncManager
from servicios.mongo_service import conectar_mongo


# ==============================
# Ventana principal
# ==============================
class App(tb.Window):
    def __init__(self):
        super().__init__(themename="cosmo")
        self.title(APP_TITLE)
        self.geometry("1120x640")
        self.minsize(920, 560)
        self.resizable(False, False)

        # estado
        self.cfg = cargar_configuracion()
        self.busy = False
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.current_thread: threading.Thread | None = None
        self.auto_mgr: AutoSyncManager | None = None

        # UI
        self._build_styles()
        self._build_layout()
        self._wire_shortcuts()

    # ---------- estilos ----------
    def _build_styles(self):
        style = tb.Style()
        style.configure("TFrame", background="#eef3f7")
        style.configure("Heading.TLabel", font=("Segoe UI", 22, "bold"), foreground="#1f2937")
        style.configure("Muted.TLabel", font=("Segoe UI", 11), foreground="#6b7280")

    # ---------- layout ----------
    def _build_layout(self):
        # Header
        self.header = tb.Frame(self, style="TFrame")
        self.header.pack(fill="x", padx=16, pady=(16, 8))

        left = tb.Frame(self.header, style="TFrame")
        left.pack(side="left")
        self.lbl_title = tb.Label(left, text="🏠  Menú Principal", style="Heading.TLabel")
        self.lbl_title.pack(anchor="w")
        sede = self.cfg.get("sede") or self.cfg.get("sede_id") or "—"
        sede_nombre = self.cfg.get("nombre_sede", "")
        tb.Label(left, text=f"Sede: {sede} · {sede_nombre}", style="Muted.TLabel").pack(anchor="w")

        right = tb.Frame(self.header, style="TFrame")
        right.pack(side="right")
        self.chip_autosync = tb.Label(
            right,
            text="🔴 Auto-Sync: Detenido",
            bootstyle="danger-inverse",
            padding=(10, 4),
        )
        self.chip_autosync.pack(side="left", padx=(0, 8))
        self.lbl_last_run = tb.Label(right, text="Última corrida: —", style="Muted.TLabel")
        self.lbl_last_run.pack(side="left", padx=(0, 8))
        self.spin_interval = tb.Spinbox(right, from_=1, to=120, width=5)
        self.spin_interval.delete(0, "end")
        self.spin_interval.insert(0, str(self.cfg.get("autosync_interval", 5)))
        self.spin_interval.pack(side="left", padx=(6, 6))
        tb.Button(right, text="⏱️ Intervalo", bootstyle="secondary", command=self._save_interval).pack(
            side="left", padx=(0, 8)
        )
        tb.Button(right, text="🌓 Tema", bootstyle="info", command=self._toggle_theme).pack(side="left")

        # Progressbar oculta
        self.progress = tb.Progressbar(self, mode="indeterminate", bootstyle="info-striped")
        self.progress.pack_forget()

        # Cuerpo principal
        main = tb.Frame(self, style="TFrame")
        main.pack(fill="both", expand=True, padx=16, pady=(4, 16))

        # Panel izquierdo (menú)
        self.left = tb.Frame(main, style="TFrame")
        self.left.pack(side="left", fill="y", padx=(0, 12))
        tb.Label(self.left, text="👋 Bienvenido", style="Muted.TLabel").pack(anchor="w", pady=(4, 8))
        tb.Button(
            self.left,
            text="🛠️  Sincronización Manual",
            width=28,
            bootstyle="primary",
            command=self._open_manual,
        ).pack(ipadx=8, ipady=8, pady=8, anchor="w")
        tb.Button(
            self.left,
            text="🔄  Sincronización Automática",
            width=28,
            bootstyle="warning",
            command=self._open_auto,
        ).pack(ipadx=8, ipady=8, pady=6, anchor="w")
        tb.Button(
            self.left,
            text="✖  Cerrar",
            width=18,
            bootstyle="danger",
            command=self._on_close,
        ).pack(ipadx=8, ipady=8, pady=12, anchor="w")

        # Panel derecho (logs)
        self.right = tb.Frame(main, style="TFrame")
        self.right.pack(side="left", fill="both", expand=True)
        bar = tb.Frame(self.right, style="TFrame")
        bar.pack(fill="x", pady=(0, 6))
        tb.Label(bar, text="🧾 Consola de logs", font=("Segoe UI", 12, "bold")).pack(side="left")
        tb.Button(bar, text="💾 Guardar", bootstyle="secondary", command=self._log_save).pack(
            side="right", padx=4
        )
        tb.Button(bar, text="📋 Copiar", bootstyle="secondary", command=self._log_copy).pack(
            side="right", padx=4
        )
        tb.Button(bar, text="🧽 Limpiar", bootstyle="secondary", command=self._log_clear).pack(
            side="right", padx=4
        )

        self.log_box = tk.Text(
            self.right,
            wrap="word",
            font=("Consolas", 10),
            relief="flat",
            bg="white",
            fg="#111827",
        )
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = tb.Scrollbar(self.right, command=self.log_box.yview, bootstyle="secondary")
        sb.pack(side="right", fill="y")
        self.log_box.config(yscrollcommand=sb.set)

        self._refresh_auto_chip()

    # ---------- tema / intervalo ----------
    def _toggle_theme(self):
        style = tb.Style()
        themes = ["cosmo", "flatly", "minty", "lumen", "sandstone", "united", "darkly", "cyborg", "solar"]
        cur = style.theme.name
        idx = (themes.index(cur) + 1) % len(themes) if cur in themes else 0
        style.theme_use(themes[idx])

    def _save_interval(self):
        try:
            mins = int(self.spin_interval.get())
            if self.auto_mgr:
                self.auto_mgr.set_interval(mins)
            self.cfg["autosync_interval"] = mins
            guardar_configuracion(self.cfg)
            messagebox.showinfo("Auto-Sync", f"Intervalo actualizado a {mins} minutos.")
        except Exception as e:
            messagebox.showerror("Auto-Sync", f"No se pudo actualizar: {e}")

    # ---------- pantallas ----------
    def _open_manual(self):
        self.lbl_title.config(text="🛠️  Control Manual")
        for w in self.left.winfo_children():
            w.destroy()

        tb.Label(self.left, text="🔧 Acciones disponibles", font=("Segoe UI", 12, "bold")).pack(
            pady=(2, 10), anchor="w"
        )

        tb.Button(
            self.left,
            text="👥 Agregar trabajadores (sede)",
            bootstyle="primary",
            width=34,
            command=lambda: self._run_async(
                self._real_agregar_trabajadores,
                "⏳ Dando de alta trabajadores…",
                "✅ Alta terminada.",
            ),
        ).pack(pady=6, anchor="w")

        tb.Button(
            self.left,
            text="🧹 Eliminar no-sede (preview + aplicar)",
            bootstyle="danger",
            width=34,
            command=lambda: self._run_async(
                self._real_eliminar_no_sede,
                "⏳ Analizando y limpiando usuarios…",
                "✅ Limpieza aplicada.",
            ),
        ).pack(pady=6, anchor="w")

        tb.Button(
            self.left,
            text="📥 Obtener asistencias (7 días)",
            bootstyle="success",
            width=34,
            command=lambda: self._run_async(
                self._real_obtener_asistencias,
                "⏳ Leyendo asistencias…",
                "✅ Lectura finalizada.",
            ),
        ).pack(pady=6, anchor="w")

        tb.Button(
            self.left,
            text="🗑️ Limpiar asistencias del checador",
            bootstyle="secondary",
            width=34,
            command=lambda: self._run_async(
                self._real_limpiar_checador,
                "⏳ Ejecutando limpieza…",
                "✅ Limpieza completada.",
            ),
        ).pack(pady=6, anchor="w")

        tb.Button(
            self.left,
            text="↩️  Volver al menú",
            bootstyle="info",
            width=34,
            command=self._open_home,
        ).pack(pady=(12, 0), anchor="w")

    def _open_auto(self):
        self.lbl_title.config(text="🔁  Sincronización Automática")
        for w in self.left.winfo_children():
            w.destroy()

        tb.Label(self.left, text="▶️ Control de Auto-Sync", font=("Segoe UI", 12, "bold")).pack(
            pady=(2, 10), anchor="w"
        )

        tb.Button(
            self.left,
            text="🟢 Detectar checador e iniciar",
            bootstyle="success",
            width=34,
            command=self._start_auto_detect,
        ).pack(pady=6, anchor="w")

        tb.Button(
            self.left,
            text="⏹️ Detener Auto-Sync",
            bootstyle="secondary",
            width=34,
            command=self._stop_auto,
        ).pack(pady=6, anchor="w")

        tb.Button(
            self.left,
            text="↩️  Volver al menú",
            bootstyle="info",
            width=34,
            command=self._open_home,
        ).pack(pady=(12, 0), anchor="w")

    def _open_home(self):
        for w in self.left.winfo_children():
            w.destroy()
        tb.Label(self.left, text="👋 Bienvenido", style="Muted.TLabel").pack(anchor="w", pady=(4, 8))
        tb.Button(
            self.left,
            text="🛠️  Sincronización Manual",
            width=28,
            bootstyle="primary",
            command=self._open_manual,
        ).pack(ipadx=8, ipady=8, pady=8, anchor="w")
        tb.Button(
            self.left,
            text="🔄  Sincronización Automática",
            width=28,
            bootstyle="warning",
            command=self._open_auto,
        ).pack(ipadx=8, ipady=8, pady=6, anchor="w")
        tb.Button(
            self.left,
            text="✖  Cerrar",
            width=18,
            bootstyle="danger",
            command=self._on_close,
        ).pack(ipadx=8, ipady=8, pady=12, anchor="w")

    # ---------- AutoSync ----------
    def _start_auto_detect(self):
        sede_id = self._get_sede_id()
        if not sede_id:
            messagebox.showwarning(
                "Auto-Sync",
                "Define la sede en configuracion_temporal.json o config.py (sede_actual)",
            )
            return

        self._log_to_both("🔎 Buscando checador en la red…")
        ip = detectar_checador(progress_callback=lambda d, t, m: self._log_to_both(m))
        if not ip:
            messagebox.showerror("Checador", "No se encontró ningún ZKTeco en la red.")
            return

        mins = int(self.spin_interval.get() or 5)
        self.auto_mgr = AutoSyncManager(
            sede_id=sede_id,
            checador_ip=ip,
            interval_min=mins,
            log_fn=self._log_to_both,
            backup_root=BACKUP_AUTO_DIR,
        )
        if self.auto_mgr.start():
            self._refresh_auto_chip()
        else:
            messagebox.showinfo("Auto-Sync", "Ya está en ejecución.")

    def _stop_auto(self):
        if self.auto_mgr and self.auto_mgr.stop():
            self._refresh_auto_chip()

    def _refresh_auto_chip(self):
        running = bool(self.auto_mgr and self.auto_mgr.is_running)
        if running:
            self.chip_autosync.configure(text="🟢 Auto-Sync: Activo", bootstyle="success-inverse")
        else:
            self.chip_autosync.configure(text="🔴 Auto-Sync: Detenido", bootstyle="danger-inverse")
        last = self.auto_mgr.last_run_utc if self.auto_mgr else None
        self.lbl_last_run.configure(
            text=f"Última corrida: {last.strftime('%d/%m %H:%M') if last else '—'}"
        )

    # ---------- Acciones MANUALES reales ----------
    def _get_sede_id(self) -> int | None:
        sede = self.cfg.get("sede") or self.cfg.get("sede_id")
        if sede is not None:
            try:
                return int(sede)
            except Exception:
                pass
        try:
            from config import sede_actual

            if sede_actual:
                return int(sede_actual)
        except Exception:
            pass
        return None

    def _detect_ip(self) -> str | None:
        self._log_to_both("🔎 Detectando checador (TCP 4370 + SDK)…")
        return detectar_checador(progress_callback=lambda d, t, m: self._log_to_both(m))

    def _real_agregar_trabajadores(self, log):
        sede_id = self._get_sede_id()
        if not sede_id:
            log("⚠️ Define la sede en configuracion_temporal.json o config.py")
            return
        agregar_trabajadores_de_sede(sede_id=sede_id, log_fn=log)

    def _real_eliminar_no_sede(self, log):
        sede_id = self._get_sede_id()
        if not sede_id:
            log("⚠️ Define la sede en configuracion_temporal.json o config.py")
            return
        try:
            log("🧪 Preview de candidatos…")
            eliminar_trabajadores_no_sede(
                sede_id=sede_id,
                log_fn=log,
                dry_run=True,
                show_details=True,
            )
        except Exception as e:
            log(f"⚠️ Preview falló: {e}")
        log("🗑️ Aplicando limpieza…")
        eliminar_trabajadores_no_sede(
            sede_id=sede_id,
            log_fn=log,
            dry_run=False,
            show_details=False,
        )

    def _real_obtener_asistencias(self, log):
        """Obtiene asistencias desde el checador, las sube a Mongo y guarda respaldo detallado."""
        sede_id = self._get_sede_id()
        if not sede_id:
            log("⚠️ Define la sede en configuracion_temporal.json o config.py")
            return

        ip = self._detect_ip()
        if not ip:
            log("❌ No se encontró checador.")
            return

        # Ventana de los últimos 7 días
        ahora = datetime.now(TZ_LOCAL)
        desde = ahora.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        hasta = (ahora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

        try:
            # Ejecutar la sincronización real
            r = obtener_asistencias(
                sede_id=sede_id,
                checador_ip=ip,
                desde=desde,
                hasta=hasta,
                log_fn=log,
            )
            ok_flag = bool(r.get("ok")) if isinstance(r, dict) else False
            insertados = r.get("insertados", 0) if isinstance(r, dict) else 0
            actualizados = r.get("actualizados", 0) if isinstance(r, dict) else 0
            eventos = r.get("eventos", 0) if isinstance(r, dict) else 0
            log(
                f"🧾 Resumen sincronización: ok={ok_flag}, "
                f"insertados={insertados}, actualizados={actualizados}, eventos={eventos}"
            )

            # Guardar respaldo completo en JSON leyendo desde Mongo
            path = self._write_backup(
                mode="manuales",
                ok=ok_flag,
                result=r if isinstance(r, dict) else {},
                desde_local=desde,
                hasta_local=hasta,
                sede_id=sede_id,
                checador_ip=ip,
            )
            log(f"💾 Respaldo manual guardado en: {path}")

        except Exception as e:
            path = self._write_backup(
                mode="manuales",
                ok=False,
                result={"error": str(e)},
                desde_local=desde,
                hasta_local=hasta,
                sede_id=sede_id,
                checador_ip=ip,
            )
            log(f"❌ Error al obtener asistencias: {e}\n💾 Bitácora guardada en: {path}")

    def _real_limpiar_checador(self, log):
        ip = self._detect_ip()
        if not ip:
            log("❌ No se encontró checador.")
            return
        try:
            limpiar_asistencias_checador(checador_ip=ip, log_fn=log)
        except Exception as e:
            log(f"⚠️ No se pudo limpiar: {e}")

    # ---------- Respaldos ----------
    def _write_backup(
        self,
        mode: str,
        ok: bool,
        result: dict,
        desde_local: datetime,
        hasta_local: datetime,
        sede_id: int,
        checador_ip: str,
    ) -> str:
        """
        Escribe un respaldo JSON.

        • Si mode == "manuales" -> usa el FORMATO LEGADO:

            {
              "sede": <int>,
              "checador_ip": "<ip>",
              "generado": "<ISO local>",
              "documentos": [ {trabajador, sede, fecha, detalle: [...]}, ... ]
            }

          tomando los documentos reales desde MongoDB (colección asistencias).

        • Si mode != "manuales" -> guarda un resumen con 'result'.

        Después de escribir, intenta comprimir carpetas viejas para no
        llenar el disco (tanto en manuales como en automaticos).
        """
        fecha_dir = datetime.now(TZ_LOCAL).strftime("%Y-%m-%d")
        base_dir = BACKUP_MANUAL_DIR if mode == "manuales" else BACKUP_AUTO_DIR
        out_dir = os.path.join(base_dir, fecha_dir)
        os.makedirs(out_dir, exist_ok=True)

        stamp = datetime.now(TZ_LOCAL).strftime("%Y%m%d_%H%M%S")
        fname = f"asistencias_{'manual' if mode=='manuales' else 'auto'}_{stamp}_sede{sede_id}_{_safe_ip(checador_ip)}.json"
        fpath = os.path.join(out_dir, fname)

        if mode == "manuales":
            # Construir DOCUMENTOS desde Mongo para dejar el respaldo igual que tus JSON viejos
            documentos: list = []
            try:
                cliente, msg = conectar_mongo()
                if not cliente:
                    self._log_to_both(
                        f"⚠️ No se pudo conectar a Mongo para respaldo detallado: {msg}"
                    )
                else:
                    db = cliente["Registro_Alu"]
                    col = db.asistencias

                    # 'fecha' en Mongo está como string YYYY-MM-DD
                    desde_str = desde_local.date().isoformat()
                    hasta_str = (hasta_local.date() - timedelta(days=1)).isoformat()

                    query = {
                        "sede": int(sede_id),
                        "fecha": {"$gte": desde_str, "$lte": hasta_str},
                    }
                    cur = col.find(query)

                    for doc in cur:
                        # quitar _id
                        doc.pop("_id", None)
                        # normalizar detalle.fechaHora a string ISO
                        detalle = doc.get("detalle")
                        if isinstance(detalle, list):
                            for ev in detalle:
                                fh = ev.get("fechaHora")
                                if isinstance(fh, datetime):
                                    ev["fechaHora"] = fh.isoformat()
                        documentos.append(doc)
            except Exception as e:
                self._log_to_both(f"⚠️ Error construyendo respaldo detallado: {e}")
                documentos = []

            payload = {
                "sede": int(sede_id),
                "checador_ip": checador_ip,
                "generado": datetime.now(TZ_LOCAL).isoformat(),
                "documentos": documentos,
            }
        else:
            # Formato resumen (por si luego lo usas para Auto-Sync)
            payload = {
                "timestamp_local": datetime.now(TZ_LOCAL).isoformat(),
                "modo": "manual" if mode == "manuales" else "auto",
                "sede_id": int(sede_id),
                "checador_ip": checador_ip,
                "ventana": {
                    "desde_local": desde_local.isoformat(),
                    "hasta_local": hasta_local.isoformat(),
                },
                "ok": bool(ok),
                "result": result or {},
            }

        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(
                    payload,
                    f,
                    ensure_ascii=False,
                    indent=2,
                    default=lambda o: o.isoformat()
                    if isinstance(o, datetime)
                    else str(o),
                )
        except Exception as e:
            self._log_to_both(f"⚠️ No se pudo escribir respaldo en {fpath}: {e}")

        # Comprimir respaldos viejos (ambos modos)
        _compress_old_backup_folders(BACKUP_MANUAL_DIR)
        _compress_old_backup_folders(BACKUP_AUTO_DIR)

        return fpath

    # ---------- logs / ejecución ----------
    def _log_to_both(self, msg: str):
        now = datetime.now(TZ_LOCAL).strftime("%H:%M:%S")
        self.log_q.put(f"[{now}] {msg}")
        self.after(0, self._flush_log_queue)

    def _flush_log_queue(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
        except queue.Empty:
            pass

    def _log_clear(self):
        self.log_box.delete("1.0", "end")

    def _log_copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self.log_box.get("1.0", "end"))
            self.update()
        except Exception:
            pass

    def _log_save(self):
        txt = self.log_box.get("1.0", "end").strip()
        if not txt:
            messagebox.showinfo("Guardar logs", "No hay contenido para guardar.")
            return
        fn = filedialog.asksaveasfilename(
            title="Guardar logs",
            defaultextension=".log",
            filetypes=[("Log", ".log"), ("Texto", ".txt"), ("Todos", ".*")],
            initialfile=f"app_{datetime.now(TZ_LOCAL).strftime('%Y%m%d_%H%M%S')}.log",
        )
        if fn:
            try:
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(txt)
                messagebox.showinfo("Guardar logs", f"Logs guardados en:\n{fn}")
            except Exception as e:
                messagebox.showerror("Guardar logs", f"No se pudo guardar: {e}")

    def _run_async(self, target_fn, start_msg: str, done_msg: str):
        if self.current_thread and self.current_thread.is_alive():
            messagebox.showinfo("Tarea en curso", "Ya hay una operación ejecutándose.")
            return

        def worker():
            try:
                target_fn(self._log_to_both)
            except Exception as e:
                self._log_to_both(f"❌ Error inesperado: {e}")
            finally:
                self._log_to_both(done_msg)
                self._set_busy(False)

        self._set_busy(True)
        self._log_to_both(start_msg)
        self.current_thread = threading.Thread(target=worker, daemon=True)
        self.current_thread.start()

    def _set_busy(self, value: bool):
        self.busy = value
        if value:
            if not self.progress.winfo_ismapped():
                self.progress.pack(fill="x", padx=16, pady=(0, 8))
            self.progress.start()
            self.configure(cursor="watch")
        else:
            self.progress.stop()
            if self.progress.winfo_ismapped():
                self.progress.pack_forget()
            self.configure(cursor="")
        self.update_idletasks()

    # ---------- atajos / cierre ----------
    def _wire_shortcuts(self):
        self.bind_all("<Control-m>", lambda e: self._open_manual())
        self.bind_all("<Control-a>", lambda e: self._open_auto())
        self.bind_all("<Control-l>", lambda e: self._log_clear())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        if self.current_thread and self.current_thread.is_alive():
            if not messagebox.askyesno(
                "Cerrar",
                "Hay una operación en curso. ¿Deseas cerrar de todos modos?",
            ):
                return
        try:
            if self.auto_mgr:
                self.auto_mgr.stop()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
