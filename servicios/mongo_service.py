from pymongo import MongoClient, errors
from shutdown_manager import register_cleanup
import time
from config import MONGO_URI  # 📌 Traemos la URI desde config.py

_cliente_global = None

def conectar_mongo(intentos=5):
    global _cliente_global

    for intento in range(1, intentos + 1):
        print(f"🔁 Intento {intento} de conexión a MongoDB...")

        try:
            if _cliente_global is None:
                _cliente_global = MongoClient(MONGO_URI, serverSelectionTimeoutMS=4000)
                # Forzar validación real
                _cliente_global.admin.command('ping')
                # Registrar cierre automático al salir
                register_cleanup(lambda: _cliente_global and _cliente_global.close())

            return _cliente_global, "✅ Conectado a MongoDB correctamente"

        except errors.ServerSelectionTimeoutError:
            mensaje = "❌ No se pudo conectar: Servidor no responde o sin internet 📴"
        except errors.ConnectionFailure:
            mensaje = "❌ Fallo de conexión: Verifica tu red o servidor fuera de línea 🌐"
        except errors.ConfigurationError:
            mensaje = "❌ URI inválida o mal configurada ⚠️"
        except errors.OperationFailure as e:
            if "Authentication failed" in str(e):
                mensaje = "❌ Fallo de autenticación: Usuario o contraseña incorrectos 🔐"
            else:
                mensaje = f"❌ Error de operación: {e}"
        except errors.InvalidURI:
            mensaje = "❌ URI inválida: Formato incorrecto 🧨"
        except Exception as e:
            mensaje = f"❌ Error desconocido: {type(e).__name__} → {e}"

        print(mensaje)
        time.sleep(1)  # Pausa entre intentos

    return None, "❌ No se pudo conectar a MongoDB. Intenta más tarde."

def obtener_sedes_completas(db):
    return list(db.sedes.find({}, {"_id": 0, "id": 1, "nombre": 1, "password": 1}))
