import tkinter as tk
from tkinter import messagebox
import json
import os

# 👉 servicio real
from servicios.trabajadores_service import agregar_trabajadores_de_sede

# Gestor de cierre global
from shutdown_manager import run_cleanup
from shutdown_manager import set_main_window, hard_exit

# =========================
# Utilidades / Config
# =========================
def cargar_configuracion():
    try:
        with open("configuracion_temporal.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"sede": "?", "nombre_sede": "No definida"}

def eliminar_configuracion_temporal():
    if os.path.exists("configuracion_temporal.json"):
        os.remove("configuracion_temporal.json")
        print("🧹 Archivo de configuración temporal eliminado.")


# =========================
# Tooltip flotante
# =========================
class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)
        widget.bind("<Motion>", self.move)

    def show(self, _=None):
        if self.tip:
            return
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)       # sin borde
        self.tip.attributes("-topmost", True)
        label = tk.Label(
            self.tip, text=self.text,
            font=("Segoe UI", 9),
            bg="#111827", fg="white",
            padx=8, pady=4
        )
        label.pack()
        # posición inicial
        X = self.widget.winfo_rootx() + 10
        Y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip.wm_geometry(f"+{X}+{Y}")

    def move(self, e):
        if not self.tip:
            return
        X = e.x_root + 10
        Y = e.y_root + 12
        self.tip.wm_geometry(f"+{X}+{Y}")

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


# =========================
# App
# =========================
config = cargar_configuracion()
ID_SEDE = config.get("sede", "?")
NOMBRE_SEDE = config.get("nombre_sede", "Desconocida")

ventana = tk.Tk()
ventana.title("Disboart - Alu Asistencias")
ventana.geometry("860x520")
ventana.configure(bg="#eef5fb")
ventana.resizable(False, False)


set_main_window(ventana)  # <- importante

# ---- Título
titulo = tk.Label(
    ventana, text="🏠 Menú Principal",
    font=("Segoe UI", 20, "bold"),
    bg="#eef5fb", fg="#1f2937"
)
titulo.pack(pady=(24, 6))

subtitulo = tk.Label(
    ventana,
    text=f"Configurado para la sede ID: {ID_SEDE} - {NOMBRE_SEDE}",
    font=("Segoe UI", 11),
    bg="#eef5fb", fg="#6b7280"
)
subtitulo.pack(pady=(0, 14))

# ---- Contenedor principal de la vista manual (se crea y oculta/enseña)
cont_manual = tk.Frame(ventana, bg="#eef5fb")
# panel izquierdo (botones)
LEFT_PANEL_W = 330
panel_izq = tk.Frame(cont_manual, bg="#eef5fb", width=LEFT_PANEL_W)
panel_izq.pack(side="left", fill="y", padx=(12, 12), pady=8)
panel_izq.pack_propagate(False)

# panel derecho (logs)
panel_der = tk.Frame(cont_manual, bg="#eef5fb")
panel_der.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=8)

# area de logs
box = tk.Text(panel_der, wrap="word", font=("Consolas", 10), relief="flat", bg="white", fg="#111827")
box.pack(side="left", fill="both", expand=True)
sb = tk.Scrollbar(panel_der, command=box.yview)
sb.pack(side="right", fill="y")
box.config(yscrollcommand=sb.set)

def log(msg: str):
    box.insert("end", msg + "\n")
    box.see("end")
    box.update_idletasks()

def limpiar_logs():
    box.delete("1.0", "end")

def cerrar_ventana():
    eliminar_configuracion_temporal()
    print("🔴 Cerrando todo...")
    hard_exit(0)   # hace cleanup + cierra ventana + termina el proceso

ventana.protocol("WM_DELETE_WINDOW", cerrar_ventana)

# =========================
# Vistas
# =========================

def abrir_sincronizacion_automatica():
    messagebox.showinfo("Sincronización Automática", "Aquí irá la lógica de sincronización automática.")

def abrir_sincronizacion_manual():
    # 🔹 Limpiar panel izquierdo antes de recrear los elementos
    for widget in panel_izq.winfo_children():
        widget.destroy()

    # Ocultar botones principales
    btn_manual.pack_forget()
    btn_auto.pack_forget()
    btn_cerrar.pack_forget()

    # Cambiar título
    titulo.config(text="🛠️ Control Manual")
    subtitulo.pack_forget()

    # Título de la vista
    header = tk.Label(
        panel_izq, text="🔧  Acciones disponibles",
        font=("Segoe UI", 12, "bold"), bg="#eef5fb", fg="#1f2937"
    )
    header.pack(pady=(2, 10), anchor="w")

    # 📌 Crear botones de acciones dentro de Control Manual
    crear_boton_accion(
        "🟦 Agregar nuevos trabajadores",
        "#2563eb", "#1e40af",
        handler_agregar_trabajadores,
        "Agrega al checador los trabajadores activos de esta sede con id_checador > 100."
    )
    crear_boton_accion(
        "🟥 Eliminar trabajadores que no sean de la sede",
        "#dc2626", "#991b1b",
        handler_eliminar_no_sede,
        "Quita del checador IDs que no pertenezcan a esta sede (no admins)."
    )
    crear_boton_accion(
        "🟩 Obtener asistencias",
        "#16a34a", "#166534",
        handler_obtener_asistencias,
        "Lee registros del checador y los guarda en Mongo (sin duplicados)."
    )
    crear_boton_accion(
        "🧹 Limpieza del checador",
        "#374151", "#1f2937",
        handler_limpieza_checador,
        "Borra asistencias almacenadas en el checador (Obten asistencias primero)."
    )
    crear_boton_accion(
        "🧹 Limpiar Consola",
        "#6b7280", "#4b5563",
        limpiar_logs,
        "Borra todo el contenido de los logs mostrados a la derecha."
    )
    crear_boton_accion(
        "↩️  Salir de sincronización manual",
        "#6b7280", "#4b5563",
        volver_menu,
        "Regresar al menú principal."
    )
    
    cont_manual.pack(fill="both", expand=True)

def volver_menu():
    # limpiar logs? (opcional)
    # box.delete("1.0", "end")

    # Ocultar vista manual
    cont_manual.pack_forget()

    # Restaurar botones principales
    titulo.config(text="🏠 Menú Principal")
    subtitulo.pack(pady=(0, 14))
    btn_manual.pack(pady=10)
    btn_auto.pack(pady=6)
    btn_cerrar.pack(pady=12)

# =========================
# Botones del menú principal
# =========================
btn_manual = tk.Button(
    ventana, text="🛠️ Sincronización Manual",
    font=("Segoe UI", 11, "bold"),
    bg="#2563eb", fg="white",
    activebackground="#1e40af", activeforeground="white",
    width=28, relief="flat",
    command=abrir_sincronizacion_manual
)
btn_manual.pack(pady=10)

btn_auto = tk.Button(
    ventana, text="🔄 Sincronización Automática",
    font=("Segoe UI", 11, "bold"),
    bg="#f59e0b", fg="white",
    activebackground="#b45309", activeforeground="white",
    width=28, relief="flat",
    command=abrir_sincronizacion_automatica
)
btn_auto.pack(pady=6)

btn_cerrar = tk.Button(
    ventana, text="❌ Cerrar",
    font=("Segoe UI", 11, "bold"),
    bg="#ef4444", fg="white",
    activebackground="#991b1b", activeforeground="white",
    relief="flat", padx=18, pady=6,
    command=cerrar_ventana
)
btn_cerrar.pack(pady=12)

# =========================
# Botones acciones (panel izq)
# =========================
def crear_boton_accion(texto, color_bg, color_active, comando, tooltip):
    btn = tk.Button(
        panel_izq, text=texto,
        font=("Segoe UI", 10, "bold"),
        bg=color_bg, fg="white",
        activebackground=color_active, activeforeground="white",
        relief="flat", pady=8, anchor="w", padx=14, width=32, cursor="hand2",
        command=comando
    )
    btn.pack(pady=6, anchor="w")
    Tooltip(btn, tooltip)
    return btn

def handler_agregar_trabajadores():
    log("🚀 Iniciando alta de trabajadores...")
    try:
        agregar_trabajadores_de_sede(sede_id=ID_SEDE, log_fn=log)
    except Exception as e:
        log(f"❌ Error inesperado: {e}")

def handler_eliminar_no_sede():
    from tkinter import messagebox
    from servicios.trabajadores_service import eliminar_trabajadores_no_sede

    log("🧹 Limpieza: eliminar IDs que no son de esta sede (auto-detección admins)…")

    # 1) Confirmación inicial
    if not messagebox.askyesno(
        "Confirmar limpieza",
        "Se eliminarán del checador los usuarios que NO pertenezcan a esta sede y NO sean administradores.\n\n¿Continuar?"
    ):
        log("❎ Operación cancelada por el usuario.")
        return

    # 2) PREVIEW (detallado, solo una vez)
    try:
        preview = eliminar_trabajadores_no_sede(
            sede_id=ID_SEDE,
            log_fn=log,
            dry_run=True,
            show_details=True,          # muestra admins, sede con nombres, candidatos
            preset_candidatos=None      # se calcula la lista
        )
    except Exception as e:
        log(f"❌ Error en DRY-RUN: {e}")
        return

    candidatos = preview.get("candidatos", [])
    if not candidatos:
        log("✨ No hay nada que eliminar. Checador limpio.")
        return

    # 3) Confirmación final (ya sabemos cuántos son)
    if not messagebox.askokcancel(
        "Eliminar definitivamente",
        f"Se eliminarán {len(candidatos)} ID(s) del checador.\nEsto NO modifica MongoDB.\n\n¿Eliminar ahora?"
    ):
        log("❎ Operación cancelada en confirmación final.")
        return

    # 4) APLICAR (silencioso: no repite admins/sede/etc., solo imprime cada borrado + resumen)
    try:
        eliminar_trabajadores_no_sede(
            sede_id=ID_SEDE,
            log_fn=log,
            dry_run=False,
            show_details=False,          # no repetir todo el detalle
            preset_candidatos=candidatos # reutiliza la lista detectada
        )
    except Exception as e:
        log(f"❌ Error inesperado: {e}")

def handler_obtener_asistencias():
    log("📥 (pendiente) Obtener asistencias + correcciones...")

def handler_limpieza_checador():
    log("🗑️ (pendiente) Limpieza de asistencias en checador...")

if __name__ == "__main__":
    ventana.mainloop()
