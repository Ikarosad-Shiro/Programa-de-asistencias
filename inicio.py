import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as messagebox
import sys
import time
import json
import os

from servicios.mongo_service import conectar_mongo, obtener_sedes_completas
from servicios.checador_service import conectar_checador_y_usuarios
from servicios.sede_service import detectar_sede_por_checador
from shutdown_manager import set_main_window, hard_exit, run_cleanup

# -------------------------------
# Variables globales
# -------------------------------
cliente = None
sede_actual = None  # Se guarda la sede seleccionada (temporalmente)

# 🪟 Ventana principal
ventana = tk.Tk()
ventana.title("Disboart - Alu Asistencias")
ventana.geometry("600x400")
ventana.configure(bg="#f0f4f8")
ventana.resizable(False, False)

set_main_window(ventana)

# 🧠 Funciones principales
def iniciar_configuracion():
    global cliente

    # 🟢 Conexión a MongoDB
    estado_mongo_label.config(text="⏳ Intentando conectar a MongoDB...", fg="#555")
    estado_checador_label.config(text="Estado Checador: ⏳ Esperando conexión...", fg="#555")
    progress.pack()
    progress.start()
    ventana.update()

    cliente, mensaje = conectar_mongo()

    progress.stop()
    progress.pack_forget()

    if cliente:
        estado_mongo_label.config(text=mensaje, fg="#006400")
        ventana.update()
        time.sleep(1)
    else:
        estado_mongo_label.config(text=mensaje, fg="#990000")
        return

    # 📡 Buscar checador
    estado_checador_label.config(text="🔎 Buscando checador en red local...", fg="#555")
    progress_checad.pack()
    progress_checad.start()
    ventana.update()

    usuarios, ip = conectar_checador_y_usuarios()

    progress_checad.stop()
    progress_checad.pack_forget()

    if ip:
        estado_checador_label.config(
            text=f"✅ Checador detectado en {ip} ({len(usuarios)} usuarios)", fg="#006400"
        )
    else:
        estado_checador_label.config(
            text="❌ No se encontró ningún checador disponible", fg="#990000"
        )
        return

    # 👥 Comparar usuarios vs trabajadores Mongo
    try:
        trabajadores = list(cliente["Registro_Alu"].trabajadores.find({}))
        resultado = detectar_sede_por_checador(usuarios, trabajadores)

        # 🔒 Soporta ambas versiones del detector:
        # - Nueva: usa 'decision' (auto/debil/manual)
        # - Antigua: usa solo 'porcentaje'
        if not resultado:
            mostrar_formulario_manual()
            return

        # Log útil para diagnóstico
        if "decision" in resultado:
            print("🔎 Detector:", {
                "decision": resultado.get("decision"),
                "known/total": f"{resultado.get('known_ids', 0)}/{resultado.get('total_ids', 0)}",
                "top3": resultado.get("top3", [])
            })
            if resultado["decision"] == "manual":
                mostrar_formulario_manual()
            else:
                confirmar_sede_detectada(resultado["sede"], resultado.get("porcentaje", 0.0))
        else:
            # Fallback a tu umbral anterior
            if resultado.get("porcentaje", 0) < 30:
                mostrar_formulario_manual()
            else:
                confirmar_sede_detectada(resultado["sede"], resultado["porcentaje"])

    except Exception as e:
        print(f"❌ Error al obtener trabajadores o detectar sede: {e}")
        mostrar_formulario_manual()

def salir():
    try:
        run_cleanup()
    finally:
        hard_exit(0)

def confirmar_sede_detectada(sede_id, porcentaje):
    global cliente
    try:
        db = cliente["Registro_Alu"]
        sede_obj = db.sedes.find_one({"id": sede_id}, {"_id": 0, "nombre": 1})
        sede_nombre = sede_obj["nombre"] if sede_obj else f"Sede {sede_id}"
    except Exception as e:
        print("❌ Error obteniendo nombre de sede:", e)
        sede_nombre = f"Sede {sede_id}"

    ventana_confirm = tk.Toplevel(ventana)
    ventana_confirm.title("Sede detectada automáticamente")
    ventana_confirm.geometry("450x250")
    ventana_confirm.configure(bg="white")
    ventana_confirm.resizable(False, False)
    ventana_confirm.grab_set()

    titulo = tk.Label(
        ventana_confirm,
        text="Sede detectada automáticamente ✅",
        font=("Segoe UI", 12, "bold"),
        bg="white",
        fg="#1a73e8"
    )
    titulo.pack(pady=(15, 5))

    mensaje = (
        f"Se detectó automáticamente que este checador pertenece\n"
        f"a la sede '{sede_nombre}' con un {porcentaje:.1f}% de coincidencia.\n\n"
        f"Todos los trabajadores y registros se sincronizarán con esta sede.\n\n"
        f"¿Deseas continuar con esta configuración?"
    )
    label_mensaje = tk.Label(ventana_confirm, text=mensaje, font=("Segoe UI", 10), bg="white", justify="center")
    label_mensaje.pack(pady=(10, 20))

    def confirmar():
        global sede_actual
        sede_actual = sede_id
        with open("configuracion_temporal.json", "w", encoding="utf-8") as f:
            json.dump({"sede": sede_actual, "nombre_sede": sede_nombre}, f, ensure_ascii=False)
        print(f"📌 Sede confirmada automáticamente: {sede_actual}")
        ventana_confirm.destroy()
        ventana.destroy()
        os.execl(sys.executable, sys.executable, "menu.py")

    def cancelar():
        ventana_confirm.destroy()
        mostrar_formulario_manual()

    # 🔘 Botones
    frame_botones = tk.Frame(ventana_confirm, bg="white")
    frame_botones.pack()

    btn_si = tk.Button(
        frame_botones,
        text="✅ Sí",
        width=10,
        font=("Segoe UI", 10, "bold"),
        bg="#28a745",
        fg="white",
        command=confirmar
    )
    btn_si.grid(row=0, column=0, padx=10)

    btn_no = tk.Button(
        frame_botones,
        text="❌ No",
        width=10,
        font=("Segoe UI", 10, "bold"),
        bg="#dc3545",
        fg="white",
        command=cancelar
    )
    btn_no.grid(row=0, column=1, padx=10)

def mostrar_formulario_manual():
    print("📥 Abriendo formulario manual...")

    form = tk.Toplevel(ventana)
    form.title("🧭 Selección Manual de Sede")
    form.geometry("420x260")
    form.configure(bg="#eaf6ff")

    # 💡 Asegurar que esté en primer plano
    form.lift()
    form.attributes("-topmost", True)
    form.after(1000, lambda: form.attributes("-topmost", False))

    # 🧢 Encabezado elegante
    label_titulo = tk.Label(
        form,
        text="🏢 Selecciona la sede que usará este checador",
        font=("Segoe UI", 12, "bold"),
        bg="#eaf6ff",
        fg="#003366"
    )
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

    # 🧾 Combo elegante
    global combo
    combo = ttk.Combobox(form, values=opciones, state="readonly", font=("Segoe UI", 10))
    combo.pack(pady=5, ipadx=6, ipady=4)
    combo.current(0)

    # ✅ Botón confirmar
    def confirmar_seleccion():
        seleccion = combo.get()
        if not seleccion:
            return

        sede_id = int(seleccion.split(" - ")[0])
        sede_seleccionada = next((s for s in sedes if s["id"] == sede_id), None)
        if not sede_seleccionada:
            messagebox.showerror("Error", "No se encontró la sede seleccionada.", parent=form)
            return

        # 🔒 Pedir contraseña (definimos el callback adentro para capturar variables locales)
        def verificar_contraseña(contraseña_ingresada: str):
            if contraseña_ingresada == sede_seleccionada.get("password"):
                global sede_actual
                sede_actual = sede_id
                # 📝 Guardar configuración temporal en archivo
                with open("configuracion_temporal.json", "w", encoding="utf-8") as f:
                    json.dump(
                        {"sede": sede_actual, "nombre_sede": sede_seleccionada["nombre"]},
                        f,
                        ensure_ascii=False
                    )
                print(f"📌 Sede seleccionada manualmente: {sede_actual}")
                messagebox.showinfo("✅ Sede confirmada", "La contraseña es correcta.", parent=form)
                form.destroy()
                ventana.destroy()  # Cierra la ventana principal
                os.execl(sys.executable, sys.executable, "menu.py")
            else:
                messagebox.showerror("❌ Contraseña incorrecta", "La contraseña ingresada no es válida.", parent=form)

        pedir_contraseña_personalizada(sede_seleccionada['nombre'], verificar_contraseña)

    btn_confirmar = ttk.Button(form, text="✅ Confirmar selección", command=confirmar_seleccion)
    btn_confirmar.pack(pady=(20, 10), ipadx=8, ipady=5)

def pedir_contraseña_personalizada(sede_nombre, callback_confirmacion):
    form_pass = tk.Toplevel(ventana)
    form_pass.title("🔒 Contraseña requerida")
    form_pass.geometry("380x180")
    form_pass.configure(bg="#f0f4f8")
    form_pass.resizable(False, False)
    form_pass.grab_set()

    tk.Label(
        form_pass,
        text=f"Ingrese la contraseña para la sede '{sede_nombre}':",
        font=("Segoe UI", 10),
        bg="#f0f4f8",
        wraplength=340,
        justify="left"
    ).pack(pady=(20, 10))

    entry_pass = ttk.Entry(form_pass, show="*", font=("Segoe UI", 11))
    entry_pass.pack(pady=5, ipadx=5, ipady=3)

    def confirmar():
        contraseña = entry_pass.get()
        form_pass.destroy()  # 🧼 Cierra primero la ventana actual
        callback_confirmacion(contraseña)  # 🔁 Luego ejecuta la lógica

    def cancelar():
        form_pass.destroy()

    botones = tk.Frame(form_pass, bg="#f0f4f8")
    botones.pack(pady=(15, 5))

    ttk.Button(botones, text="✅ Confirmar", command=confirmar).grid(row=0, column=0, padx=10)
    ttk.Button(botones, text="❌ Cancelar", command=cancelar).grid(row=0, column=1, padx=10)

# 🎨 Estilo
estilo = ttk.Style()
estilo.theme_use("clam")
estilo.configure(
    "TButton",
    font=("Segoe UI", 11),
    padding=10,
    foreground="#fff",
    background="#007acc"
)
estilo.map("TButton", background=[("active", "#005f99")])

estilo.configure(
    "green.Horizontal.TProgressbar",
    troughcolor="#e0e0e0",
    background="#4caf50",
    thickness=20
)

# 🖼️ UI General
titulo = tk.Label(
    ventana,
    text="🚀 Servidor Local - Alu Asistencias",
    font=("Segoe UI", 16, "bold"),
    bg="#f0f4f8",
    fg="#333"
)
titulo.pack(pady=(30, 10))

subtitulo = tk.Label(
    ventana,
    text="Sistema de asistencia empresarial",
    font=("Segoe UI", 11),
    bg="#f0f4f8",
    fg="#555"
)
subtitulo.pack()

btn_iniciar = ttk.Button(ventana, text="⚙️ Iniciar configuración Inicial", command=iniciar_configuracion)
btn_iniciar.pack(pady=20)

btn_salir = ttk.Button(ventana, text="❌ Salir", command=salir)
btn_salir.pack(pady=10)

estado_mongo_label = tk.Label(
    ventana,
    text="Estado MongoDB: ❌ Sin conexión",
    font=("Segoe UI", 10, "italic"),
    bg="#f0f4f8",
    fg="#990000"
)
estado_mongo_label.pack(side="bottom", pady=(5, 2))

estado_checador_label = tk.Label(
    ventana,
    text="Estado Checador: ⏳ Esperando conexión...",
    font=("Segoe UI", 10, "italic"),
    bg="#f0f4f8",
    fg="#555"
)
estado_checador_label.pack(side="bottom", pady=(0, 10))

progress = ttk.Progressbar(ventana, style="green.Horizontal.TProgressbar", mode="indeterminate", length=300)
progress.pack(pady=(5, 5))
progress.pack_forget()

progress_checad = ttk.Progressbar(ventana, style="green.Horizontal.TProgressbar", mode="indeterminate", length=300)
progress_checad.pack(pady=(5, 5))
progress_checad.pack_forget()

# 🚪 Manejo de cierre correcto (colócalo ANTES del mainloop)
ventana.protocol("WM_DELETE_WINDOW", salir)

# 🚀 Iniciar la interfaz
if __name__ == "__main__":
    ventana.mainloop()
