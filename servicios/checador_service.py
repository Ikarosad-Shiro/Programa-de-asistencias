from zk import ZK
import socket
import time
from shutdown_manager import register_cleanup  # 👈 nuevo

IPS = [f"192.168.{net}.{i}" for net in (0, 1, 2) for i in range(101, 106)]
PUERTO = 4370

def conectar_checador_y_usuarios(intentos=5):
    for intento in range(1, intentos + 1):
        print(f"🔁 Intento {intento} de conexión con checadores...")

        for ip in IPS:
            try:
                print(f"🔌 Buscando checador en {ip}...")
                zk = ZK(ip, port=PUERTO, timeout=5)

                try:
                    conn = zk.connect()
                except Exception as e:
                    print(f"❌ No se pudo conectar a {ip} → {e}")
                    continue

                try:
                    users = conn.get_users()
                except Exception as e:
                    print(f"⚠️ Conectado a {ip} pero falló al obtener usuarios → {e}")
                    conn.disconnect()
                    continue

                conn.disconnect()
                print(f"✅ Checador conectado en {ip} con {len(users)} usuarios.")
                return users, ip

            except socket.timeout:
                print(f"⏰ Timeout en {ip}")
            except socket.error as e:
                print(f"🌐 Error de red con {ip}: {e}")
            except Exception as e:
                print(f"🧨 Error inesperado en {ip}: {type(e).__name__} → {e}")
                continue

        time.sleep(1)  # 💤 Reducir intensidad entre intentos

    # Si ningún checador respondió
    print("❌ No se pudo conectar a ningún checador después de varios intentos.")
    return [], None

def conectar_checador(intentos=3, espera=2):
    for intento in range(1, intentos + 1):
        print(f"🔁 Intento {intento} de conexión directa al checador...")
        for ip in IPS:
            try:
                zk = ZK(ip, port=PUERTO, timeout=8)
                conn = zk.connect()
                print(f"✅ Conectado a checador en {ip}")

                # 👇 registramos cierre por si el caller se olvida
                register_cleanup(lambda c=conn: c and c.disconnect())

                return conn, ip
            except socket.timeout:
                print(f"⏰ Timeout en {ip}")
            except Exception as e:
                print(f"❌ No se pudo conectar a {ip}: {e}")
                continue
        time.sleep(espera)
    print("🚫 No se encontró checador disponible.")
    return None, None