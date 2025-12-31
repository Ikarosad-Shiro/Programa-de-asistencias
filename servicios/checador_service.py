# checador_service.py
from zk import ZK
import socket
import time
from typing import Callable, Iterable, Optional, Tuple, List
from threading import Event
from shutdown_manager import register_cleanup

# ===== Parámetros por defecto (ajustados a tu rango original) =====
PUERTO = 4370
DEFAULT_NETS = (0, 1, 2)                # 192.168.0.x, 1.x, 2.x
DEFAULT_HOSTS = range(101, 107)         # .101 a .106
TCP_TIMEOUT = 1.0                        # seg. para chequeo rápido de puerto
ZK_TIMEOUT = 5                           # seg. para el SDK ZKTeco

# ===== Helpers =====
def _gen_ips(nets: Iterable[int] = DEFAULT_NETS,
             hosts: Iterable[int] = DEFAULT_HOSTS) -> List[str]:
    """Genera la lista de IPs a escanear."""
    return [f"192.168.{n}.{h}" for n in nets for h in hosts]

def _puerto_abierto(ip: str, puerto: int = PUERTO, timeout: float = TCP_TIMEOUT) -> bool:
    """Chequeo rápido de puerto TCP para filtrar IPs sin equipo."""
    try:
        with socket.create_connection((ip, puerto), timeout=timeout):
            return True
    except Exception:
        return False

def _emit(progress_callback: Optional[Callable[[int, int, str], None]],
          done: int, total: int, msg: str) -> None:
    if progress_callback:
        progress_callback(done, total, msg)

# --- Compatibilidad con código heredado (menu.py / trabajadores_service.py) ---
IPS = _gen_ips()  # para que 'from servicios.checador_service import IPS, PUERTO' siga funcionando

# ===== API principal =====
def detectar_checador(progress_callback: Optional[Callable[[int, int, str], None]] = None,
                      stop_event: Optional[Event] = None,
                      nets: Iterable[int] = DEFAULT_NETS,
                      hosts: Iterable[int] = DEFAULT_HOSTS,
                      #DEFAULT_NETS = (0, 1, 2),
                      #DEFAULT_HOSTS = range(101, 107),
                      use_tcp_prefilter: bool = True) -> Optional[str]:
    """
    Escanea la red y devuelve la primera IP de checador disponible (validada con SDK).
    Si use_tcp_prefilter=True, primero hace un pre-check TCP 4370 para acelerar.
    """
    ips = _gen_ips(nets, hosts)
    total = len(ips)

    for i, ip in enumerate(ips, start=1):
        if stop_event and stop_event.is_set():
            _emit(progress_callback, i, total, "⛔ Escaneo cancelado por el usuario.")
            return None

        _emit(progress_callback, i, total, f"🔎 Probando {ip} (puerto {PUERTO})...")

        # 1) Filtro TCP (opcional)
        if use_tcp_prefilter:
            if not _puerto_abierto(ip):
                _emit(progress_callback, i, total, f"❌ {ip}: puerto cerrado / sin respuesta.")
                continue
            else:
                _emit(progress_callback, i, total, f"🟢 {ip}: puerto 4370 abierto, probando SDK…")

        # 2) Verificación con SDK (ligera)
        try:
            zk = ZK(ip, port=PUERTO, timeout=ZK_TIMEOUT)
            conn = zk.connect()
            try:
                # Llamada ligera para confirmar que es un ZKTeco real
                try:
                    fn = getattr(conn, "get_firmware_version", None)
                    if fn:
                        fn()
                except Exception:
                    # A veces firmware no está disponible; intentamos algo rápido
                    try:
                        conn.test_voice()
                    except Exception:
                        pass
                _emit(progress_callback, i, total, f"✅ Checador detectado en {ip}")
                return ip
            finally:
                try:
                    conn.disconnect()
                except Exception:
                    pass
        except Exception as e:
            _emit(progress_callback, i, total, f"⚠️ {ip}: puerto responde pero SDK falló → {e}")
            continue

    _emit(progress_callback, total, total, "🚫 No se encontró ningún checador.")
    return None

def conectar_checador(intentos: int = 2,
                      espera: float = 1.0,
                      progress_callback: Optional[Callable[[int, int, str], None]] = None,
                      stop_event: Optional[Event] = None,
                      nets: Iterable[int] = DEFAULT_NETS,
                      hosts: Iterable[int] = DEFAULT_HOSTS,
                      use_tcp_prefilter: bool = True) -> Tuple[Optional[object], Optional[str]]:
    """
    Devuelve (conn, ip) con la conexión abierta al checador, o (None, None).
    Registra cleanup automático por si el caller olvida desconectar.
    """
    for intento in range(1, intentos + 1):
        _emit(progress_callback, 0, 1, f"🔁 Intento {intento}: buscando checador...")
        ip = detectar_checador(progress_callback=progress_callback, stop_event=stop_event,
                               nets=nets, hosts=hosts, use_tcp_prefilter=use_tcp_prefilter)
        if ip:
            try:
                zk = ZK(ip, port=PUERTO, timeout=ZK_TIMEOUT)
                conn = zk.connect()
                register_cleanup(lambda c=conn: c and c.disconnect())
                _emit(progress_callback, 1, 1, f"✅ Conectado a checador en {ip}")
                return conn, ip
            except Exception as e:
                _emit(progress_callback, 1, 1, f"❌ Falló la conexión final a {ip}: {e}")
        if stop_event and stop_event.is_set():
            _emit(progress_callback, 1, 1, "⛔ Conexión cancelada.")
            return None, None
        time.sleep(espera)

    _emit(progress_callback, 1, 1, "🚫 No fue posible conectar a ningún checador.")
    return None, None

def conectar_checador_y_usuarios(intentos: int = 2,
                                 espera: float = 1.0,
                                 progress_callback: Optional[Callable[[int, int, str], None]] = None,
                                 stop_event: Optional[Event] = None,
                                 nets: Iterable[int] = DEFAULT_NETS,
                                 hosts: Iterable[int] = DEFAULT_HOSTS,
                                 use_tcp_prefilter: bool = True) -> Tuple[List[object], Optional[str]]:
    """
    Variante que devuelve (users, ip). Cierra la conexión después de leer usuarios.
    """
    conn, ip = conectar_checador(intentos=intentos, espera=espera,
                                 progress_callback=progress_callback,
                                 stop_event=stop_event, nets=nets, hosts=hosts,
                                 use_tcp_prefilter=use_tcp_prefilter)
    if not conn or not ip:
        return [], None
    try:
        users = conn.get_users()
        return users, ip
    except Exception as e:
        _emit(progress_callback, 1, 1, f"⚠️ Conectado a {ip} pero falló get_users(): {e}")
        return [], ip
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass
