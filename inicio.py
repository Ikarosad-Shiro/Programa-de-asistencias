import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as messagebox
import sys
import time
import json
import os

import ttkbootstrap as tb
from ttkbootstrap.constants import *
from threading import Thread, Event

from servicios.mongo_service import conectar_mongo, obtener_sedes_completas
from servicios.checador_service import conectar_checador_y_usuarios
from servicios.sede_service import detectar_sede_por_checador
from shutdown_manager import set_main_window, hard_exit, run_cleanup

# -------------------------------
# Variables globales
# -------------------------------
cliente = None
sede_actual = None  # Se guarda la sede seleccionada (temporalmente)
stop_event = Event()  # permite cancelar el escaneo desde la UI

# 🪟 Ventana principal (tema moderno)
ventana = tb.Window(themename="cosmo")  # otros: "darkly", "minty", "solar", "cyborg"
ventana.title("Disboart - Alu Asistencias")
ventana.geometry("860x520")
ventana.resizable(False, False)
set_main_window(ventana)

# ===== Estética global =====
LIGHT_BG = "#f5f6f7"  # gris clarito tipo bg-light
MUTED   = "#6c757d"
SUCCESS = "#198754"
DANGER  = "#dc3545"

ventana.configure(background=LIGHT_BG)

style = tb.Style()
# Herencia de fondo gris para todo
style.configure("TFrame", background=LIGHT_BG)
style.configure("TLabel", background=LIGHT_BG)

# Estilos propios
style.configure("Card.TFrame", background=LIGHT_BG)  # "card" sin blanco
style.configure("Title.TLabel",  background=LIGHT_BG, font=("Segoe UI", 20, "bold"))
style.configure("Muted.TLabel",  background=LIGHT_BG, foreground=MUTED,   font=("Segoe UI", 11))
style.configure("Success.TLabel", background=LIGHT_BG, foreground=SUCCESS, font=("Segoe UI", 10, "italic"))
style.configure("Danger.TLabel",  background=LIGHT_BG, foreground=DANGER,  font=("Segoe UI", 10, "italic"))

# Neutralizar halo/contorno de enfoque
try:
    style.configure("TButton", focuscolor=LIGHT_BG)
except Exception:
    pass
ventana.option_add('*highlightThickness', 0)

# ===== Helpers de UI =====
def _set_ui_busqueda_en_progreso(en_progreso: bool):
    """Habilita/Deshabilita controles y muestra/oculta elementos durante la búsqueda."""
    if en_progreso:
        btn_iniciar.config(state="disabled")
        btn_salir.config(state="disabled")
        progress_checad['value'] = 0
        progress_checad.grid()
        btn_cancelar.grid()
    else:
        btn_iniciar.config(state="normal")
        btn_salir.config(state="normal")
        progress_checad.grid_remove()
        btn_cancelar.grid_remove()

def on_progress(done: int, total: int, msg: str):
    """Recibe el avance desde checador_service y actualiza la UI."""
    try:
        if total > 0:
            progress_checad['maximum'] = total
            progress_checad['value'] = done
        estado_checador_label.config(text=msg, style="Muted.TLabel")
        ventana.update_idletasks()
    except Exception:
        pass  # evitar que un cierre de ventana rompa el callback

def cancelar_busqueda():
    stop_event.set()
    estado_checador_label.config(text="⛔ Escaneo cancelado por el usuario.", style="Danger.TLabel")

# 🧠 Funciones principales
def iniciar_configuracion():
    global cliente

    # 🟢 Conexión a MongoDB
    estado_mongo_label.config(text="⏳ Intentando conectar a MongoDB...", style="Muted.TLabel")
    estado_checador_label.config(text="Estado Checador: ⏳ Esperando conexión...", style="Muted.TLabel")
    progress.pack()
    progress.start()
    ventana.update()

    cliente, mensaje = conectar_mongo()

    progress.stop()
    progress.pack_forget()

    if cliente:
        estado_mongo_label.config(text=mensaje, style="Success.TLabel")
        ventana.update()
        time.sleep(1)
    else:
        estado_mongo_label.config(text=mensaje, style="Danger.TLabel")
        return

    # 📡 Buscar checador (con progreso y cancelación)
    estado_checador_label.config(text="🔎 Buscando checador en red local...", style="Muted.TLabel")
    stop_event.clear()
    _set_ui_busqueda_en_progreso(True)

    def tarea_busqueda():
        try:
            # 1) Intento directo a tu IP conocida (sin pre-filtro TCP)
            usuarios, ip = conectar_checador_y_usuarios(
                progress_callback=on_progress,
                stop_event=stop_event,
                nets=(1,),              # Solo red 192.168.1.x
                hosts=(101,),           # Solo 192.168.1.101
                use_tcp_prefilter=False
            )

            # 2) Si no se encontró, escaneo corto de respaldo
            if not ip and not stop_event.is_set():
                usuarios, ip = conectar_checador_y_usuarios(
                    progress_callback=on_progress,
                    stop_event=stop_event,
                    nets=(1,),                    # 192.168.1.x
                    hosts=tuple(range(100, 111)), # 100-110
                    use_tcp_prefilter=True
                )

        except Exception as e:
            usuarios, ip = [], None
            print(f"❌ Error buscando checador: {e}")

        # Volvemos al hilo principal para actualizar UI/flujo
        def continuar():
            _set_ui_busqueda_en_progreso(False)
            if ip:
                estado_checador_label.config(
                    text=f"✅ Checador detectado en {ip} ({len(usuarios)} usuarios)",
                    style="Success.TLabel"
                )
                # 👥 Comparar usuarios vs trabajadores Mongo
                try:
                    trabajadores = list(cliente["Registro_Alu"].trabajadores.find({}))
                    resultado = detectar_sede_por_checador(usuarios, trabajadores)

                    if not resultado:
                        mostrar_formulario_manual()
                        return

                    if "decision" in resultado:
                        print("🔎 Detector:", {
                            "decision": resultado.get("decision"),
                            "known/total": f"{resultado.get('known_ids', 0)}/{resultado.get('total_ids', 0)}",
                            "top3": resultado.get("top3", [])
                        })
                        if resultado["decision"] == "manual":
                            mostrar_formulario_manual()
                        else:
                            confirmar_sede_detectada(
                                resultado["sede"],
                                resultado.get("porcentaje", 0.0),
                                checador_ip=ip
                            )
                    else:
                        if resultado.get("porcentaje", 0) < 30:
                            mostrar_formulario_manual()
                        else:
                            confirmar_sede_detectada(
                                resultado["sede"],
                                resultado["porcentaje"],
                                checador_ip=ip
                            )
                except Exception as e:
                    print(f"❌ Error al obtener trabajadores o detectar sede: {e}")
                    mostrar_formulario_manual()
            else:
                if stop_event.is_set():
                    estado_checador_label.config(text="⛔ Búsqueda cancelada.", style="Danger.TLabel")
                else:
                    estado_checador_label.config(text="❌ No se encontró ningún checador disponible", style="Danger.TLabel")

        ventana.after(0, continuar)

    Thread(target=tarea_busqueda, daemon=True).start()

def salir():
    try:
        run_cleanup()
    finally:
        hard_exit(0)

def confirmar_sede_detectada(sede_id, porcentaje, checador_ip):
    """Modal elegante, centrado y con mejor presentación visual."""
    global cliente
    try:
        db = cliente["Registro_Alu"]
        sede_obj = db.sedes.find_one({"id": sede_id}, {"_id": 0, "nombre": 1})
        sede_nombre = sede_obj["nombre"] if sede_obj else f"Sede {sede_id}"
    except Exception as e:
        print("❌ Error obteniendo nombre de sede:", e)
        sede_nombre = f"Sede {sede_id}"

    # --- Ventana modal ---
    win = tb.Toplevel(ventana)
    win.title("Sede detectada automáticamente")
    win.resizable(False, False)
    win.transient(ventana)
    win.grab_set()
    win.focus_force()
    win.attributes("-topmost", True)
    win.after(200, lambda: win.attributes("-topmost", False))
    win.configure(background=LIGHT_BG)

    # --- Card principal ---
    card = tb.Frame(win, padding=(32, 28), style="Card.TFrame")
    card.grid(row=0, column=0, sticky="nsew")
    win.grid_columnconfigure(0, weight=1)
    win.grid_rowconfigure(0, weight=1)

    # --- Título ---
    titulo = tb.Label(
        card,
        text="Sede detectada automáticamente ✅",
        font=("Segoe UI Semibold", 14, "bold"),
        background=LIGHT_BG,
        foreground="#212529",
        anchor="w"
    )
    titulo.grid(row=0, column=0, sticky="w", pady=(6, 12))

    # --- Cuerpo de mensaje ---
    msg = (
        f"Se detectó automáticamente que este checador pertenece a la sede "
        f"“{sede_nombre}” con un {porcentaje:.1f}% de coincidencia.\n\n"
        f"Todos los trabajadores y registros se sincronizarán con esta sede.\n\n"
        f"¿Deseas continuar con esta configuración?"
    )

    cuerpo = tb.Label(
        card,
        text=msg,
        font=("Segoe UI", 11),
        justify="left",
        anchor="w",
        wraplength=440,
        background=LIGHT_BG,
        foreground="#333333"
    )
    cuerpo.grid(row=1, column=0, sticky="ew", pady=(0, 18))

    # --- Separador visual ---
    tb.Separator(card).grid(row=2, column=0, sticky="ew", pady=(0, 16))

    # --- Botonera ---
    botones = tb.Frame(card, style="Card.TFrame")
    botones.grid(row=3, column=0, sticky="e")

    def confirmar():
        global sede_actual
        sede_actual = sede_id
        data = {
            "sede": sede_actual,
            "nombre_sede": sede_nombre,
            "setup_done": True,
            "checador_ip": checador_ip,
            "ts": time.time()
        }
        with open("configuracion_temporal.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print(f"📌 Sede confirmada automáticamente: {sede_actual} | IP: {checador_ip}")
        win.destroy()
        ventana.destroy()
        os.execl(sys.executable, sys.executable, "menu.py")

    def cancelar():
        win.destroy()
        mostrar_formulario_manual()

    # --- Botones con estilo simétrico ---
    tb.Button(botones, text="❌ No", bootstyle="danger-outline", width=10, command=cancelar)\
        .grid(row=0, column=0, padx=(0, 10))
    tb.Button(botones, text="✅ Sí", bootstyle="success", width=10, command=confirmar)\
        .grid(row=0, column=1)

    # --- Centrado natural ---
    win.update_idletasks()
    try:
        x = ventana.winfo_rootx() + (ventana.winfo_width() - win.winfo_width()) // 2
        y = ventana.winfo_rooty() + (ventana.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
    except Exception:
        pass


def mostrar_formulario_manual():
    print("📥 Abriendo formulario manual...")

    form = tb.Toplevel(ventana)
    form.title("🧭 Selección Manual de Sede")
    form.geometry("420x260")
    form.resizable(False, False)

    label_titulo = tb.Label(form, text="🏢 Selecciona la sede que usará este checador",
                            font=("Segoe UI", 12, "bold"), background=LIGHT_BG)
    label_titulo.pack(pady=(20, 10))

    # 📥 Obtener sedes
    try:
        global cliente
        db = cliente["Registro_Alu"]
        print("📡 Base de datos activa:", db)
        global sedes
        sedes = obtener_sedes_completas(db)
        print("📋 Sedes cargadas:", sedes)
    except Exception as e:
        print(f"❌ Error cargando sedes: {e}")
        sedes = []

    if not sedes:
        sedes = [{"id": 0, "nombre": "Sin sedes disponibles"}]

    opciones = [f"{s['id']} - {s['nombre']}" for s in sedes]
    print("📋 Opciones generadas para combo:", opciones)

    global combo
    combo = ttk.Combobox(form, values=opciones, state="readonly", font=("Segoe UI", 10))
    combo.pack(pady=5, ipadx=6, ipady=4)
    combo.current(0)

    def confirmar_seleccion():
        seleccion = combo.get()
        if not seleccion:
            return

        sede_id = int(seleccion.split(" - ")[0])
        sede_seleccionada = next((s for s in sedes if s["id"] == sede_id), None)
        if not sede_seleccionada:
            messagebox.showerror("Error", "No se encontró la sede seleccionada.", parent=form)
            return

        def verificar_contraseña(contraseña_ingresada: str):
            if contraseña_ingresada == sede_seleccionada.get("password"):
                global sede_actual
                sede_actual = sede_id
                with open("configuracion_temporal.json", "w", encoding="utf-8") as f:
                    json.dump({"sede": sede_actual, "nombre_sede": sede_seleccionada["nombre"]},
                              f, ensure_ascii=False)
                print(f"📌 Sede seleccionada manualmente: {sede_actual}")
                messagebox.showinfo("✅ Sede confirmada", "La contraseña es correcta.", parent=form)
                form.destroy()
                ventana.destroy()
                os.execl(sys.executable, sys.executable, "menu.py")
            else:
                messagebox.showerror("❌ Contraseña incorrecta", "La contraseña ingresada no es válida.", parent=form)

        pedir_contraseña_personalizada(sede_seleccionada['nombre'], verificar_contraseña)

    tb.Button(form, text="✅ Confirmar selección", bootstyle="primary", command=confirmar_seleccion)\
        .pack(pady=(20, 10), ipadx=8, ipady=5)

def pedir_contraseña_personalizada(sede_nombre, callback_confirmacion):
    form_pass = tb.Toplevel(ventana)
    form_pass.title("🔒 Contraseña requerida")
    form_pass.geometry("380x180")
    form_pass.resizable(False, False)
    form_pass.grab_set()

    tb.Label(form_pass, text=f"Ingrese la contraseña para la sede '{sede_nombre}':",
             font=("Segoe UI", 10), wraplength=340, justify="left", background=LIGHT_BG).pack(pady=(20, 10))

    entry_pass = ttk.Entry(form_pass, show="*", font=("Segoe UI", 11))
    entry_pass.pack(pady=5, ipadx=5, ipady=3)

    def confirmar():
        contraseña = entry_pass.get()
        form_pass.destroy()
        callback_confirmacion(contraseña)

    def cancelar():
        form_pass.destroy()

    botones = tb.Frame(form_pass, style="Card.TFrame")
    botones.pack(pady=(15, 5))

    tb.Button(botones, text="✅ Confirmar", bootstyle="success", command=confirmar)\
        .grid(row=0, column=0, padx=10)
    tb.Button(botones, text="❌ Cancelar", bootstyle="danger", command=cancelar)\
        .grid(row=0, column=1, padx=10)

# ======== LAYOUT “CARD” ELEGANTE PRINCIPAL ========
# Card sobre fondo gris, con márgenes exteriores y padding interno generosos
card = tb.Frame(ventana, padding=(28, 24), style="Card.TFrame")
card.pack(expand=True, padx=28, pady=28)  # márgenes alrededor del card
card.grid_columnconfigure(0, weight=1)

titulo = tb.Label(card, text="🚀 Servidor Local · Alu Asistencias", style="Title.TLabel")
titulo.grid(row=0, column=0, pady=(6, 4))

subtitulo = tb.Label(card, text="Sistema de asistencia empresarial", style="Muted.TLabel")
subtitulo.grid(row=1, column=0, pady=(0, 16))

acciones = tb.Frame(card, style="Card.TFrame")
acciones.grid(row=2, column=0, pady=(4, 14))
acciones.grid_columnconfigure((0, 1), weight=1, uniform="cols")

btn_iniciar = tb.Button(acciones, text="⚙️  Iniciar configuración",
                        bootstyle="primary", command=iniciar_configuracion)
btn_iniciar.grid(row=0, column=0, padx=(0, 10), ipadx=10, ipady=6)

btn_salir = tb.Button(acciones, text="✖  Salir",
                      bootstyle="danger-outline", command=salir)
btn_salir.grid(row=0, column=1, padx=(10, 0), ipadx=10, ipady=6)

progress_frame = tb.Frame(card, style="Card.TFrame")
progress_frame.grid(row=3, column=0, pady=(6, 8), sticky="ew")
progress_frame.grid_columnconfigure(0, weight=1)
progress_frame.configure(borderwidth=0)

progress_checad = tb.Progressbar(progress_frame, bootstyle="success-striped",
                                 mode="determinate", length=520)
progress_checad.grid(row=0, column=0, sticky="ew")

btn_cancelar = tb.Button(progress_frame, text="⛔  Cancelar búsqueda",
                         bootstyle="warning", command=cancelar_busqueda)
btn_cancelar.grid(row=1, column=0, pady=(10, 0))

# Ocultos al inicio
progress_checad.grid_remove()
btn_cancelar.grid_remove()

estados = tb.Frame(card, style="Card.TFrame")
estados.grid(row=4, column=0, pady=(6, 2), sticky="ew")

estado_checador_label = tb.Label(estados, text="Estado Checador: ⏳ Esperando conexión…",
                                 style="Muted.TLabel")
estado_checador_label.pack(pady=(0, 4))

estado_mongo_label = tb.Label(estados, text="Estado MongoDB: ❌ Sin conexión",
                              style="Danger.TLabel")
estado_mongo_label.pack()

# Barra indeterminada para Mongo (oculta hasta usar)
progress = tb.Progressbar(ventana, mode="indeterminate", bootstyle="info-striped")
progress.pack_forget()

# 🚫 Quitar foco visual/tab-stop de botones (evita “subrayado”)
for b in (btn_iniciar, btn_salir, btn_cancelar):
    try:
        b.configure(takefocus=False)
    except Exception:
        pass

# 🚪 Manejo de cierre correcto
ventana.protocol("WM_DELETE_WINDOW", salir)

# 🚀 Iniciar la interfaz
if __name__ == "__main__":
    ventana.mainloop() 

    