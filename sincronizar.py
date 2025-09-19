import json
import os
from datetime import datetime
from servicios.checador_service import conectar_checador_y_usuarios
from servicios.mongo_service import conectar_mongo
from pymongo import UpdateOne
from zoneinfo import ZoneInfo

# 🧠 Zona horaria local
ZONA_MEXICO = ZoneInfo("America/Mexico_City")

# ✅ Cargar configuración temporal
def cargar_configuracion():
    if not os.path.exists("configuracion_temporal.json"):
        print("❌ No se encontró configuración temporal.")
        return None
    with open("configuracion_temporal.json", "r") as f:
        return json.load(f)

# 🚀 Iniciar sincronización
def sincronizar_asistencias():
    print("🔁 Iniciando sincronización manual...")

    config = cargar_configuracion()
    if not config:
        return

    sede_id = config["sede"]

    # 📡 Conectar a MongoDB
    cliente, _ = conectar_mongo()
    if not cliente:
        print("❌ No se pudo conectar a MongoDB.")
        return

    db = cliente["Registro_Alu"]

    # 📥 Conectar al checador y obtener usuarios
    registros, ip = conectar_checador_y_usuarios()
    if not registros:
        print("❌ No se encontraron registros desde el checador.")
        return

    print(f"✅ {len(registros)} registros obtenidos del checador en {ip}.")

    asistencias_por_dia = {}

    for r in registros:
        trabajador_id = str(r["id"])
        fecha_hora = datetime.fromtimestamp(r["timestamp"]).astimezone(ZONA_MEXICO)
        fecha_str = fecha_hora.strftime("%Y-%m-%d")

        tipo = "Entrada" if r["tipo"] == 0 else "Salida"  # 👈 O ajusta según modelo exacto

        clave = (trabajador_id, fecha_str)

        if clave not in asistencias_por_dia:
            asistencias_por_dia[clave] = {
                "trabajador": trabajador_id,
                "sede": sede_id,
                "fecha": fecha_str,
                "estado": "Asistencia Completa",  # Esto se puede ajustar luego
                "detalle": []
            }

        asistencias_por_dia[clave]["detalle"].append({
            "tipo": tipo,
            "fechaHora": fecha_hora.isoformat()
        })

    # 📝 Guardar/Actualizar asistencias en Mongo
    operaciones = []
    for key, asistencia in asistencias_por_dia.items():
        filtro = {"trabajador": asistencia["trabajador"], "fecha": asistencia["fecha"]}
        update = {"$set": asistencia}
        operaciones.append(UpdateOne(filtro, update, upsert=True))

    if operaciones:
        resultado = db.asistencias.bulk_write(operaciones)
        print(f"✅ Sincronización completa. {resultado.upserted_count} nuevos, {resultado.modified_count} actualizados.")
    else:
        print("⚠️ No hubo asistencias para guardar.")

if __name__ == "__main__":
    sincronizar_asistencias()
