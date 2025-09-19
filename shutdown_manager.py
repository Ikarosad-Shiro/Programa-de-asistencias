# shutdown_manager.py
import atexit, os, sys, time, signal, threading, subprocess, weakref
from typing import Callable, List, Optional

# ---- Estado global (solo recursos CREADOS por tu app) ----
_cleanup_funcs: List[Callable[[], None]] = []
_threads: List[threading.Thread] = []
_processes: List[object] = []          # multiprocessing.Process
_popens: List[subprocess.Popen] = []
_sockets: List[object] = []            # cualquier socket con .close()
_main_window_ref: Optional[weakref.ReferenceType] = None  # Tk root

# ---- Registro de recursos ----
def set_main_window(win):
    """Guarda una referencia débil a la ventana Tk principal (para cerrarla al final)."""
    global _main_window_ref
    try:
        _main_window_ref = weakref.ref(win)
    except Exception:
        _main_window_ref = None

def register_cleanup(fn: Callable[[], None]):
    if callable(fn):
        _cleanup_funcs.append(fn)

def register_thread(th: threading.Thread):
    if isinstance(th, threading.Thread):
        _threads.append(th)

def register_process(proc):
    _processes.append(proc)

def register_popen(p: subprocess.Popen):
    if isinstance(p, subprocess.Popen):
        _popens.append(p)

def register_socket(sock):
    # guarda sockets para cerrarlos al salir
    _sockets.append(sock)

# ---- Limpieza ordenada (solo lo tuyo) ----
def _close_tk_window():
    """Cierra la ventana principal si existe (sin crashear si ya no está)."""
    if _main_window_ref:
        win = _main_window_ref()
        if win is not None:
            try:
                win.quit()
            except Exception:
                pass
            try:
                win.destroy()
            except Exception:
                pass

def _run_custom_cleanups():
    for fn in list(_cleanup_funcs):
        try: fn()
        except Exception: pass
    _cleanup_funcs.clear()

def _close_sockets():
    for s in list(_sockets):
        try: s.close()
        except Exception: pass
    _sockets.clear()

def _stop_threads(timeout_total: float = 2.0):
    # si un hilo tiene .stop_event (Event), lo activamos
    for th in list(_threads):
        try:
            ev = getattr(th, "stop_event", None)
            if ev is not None:
                try: ev.set()
                except Exception: pass
        except Exception:
            pass
    # join suave
    deadline = time.time() + timeout_total
    for th in list(_threads):
        try:
            remaining = max(0.0, deadline - time.time())
            th.join(timeout=remaining / max(1, len(_threads)))
        except Exception:
            pass
    _threads.clear()

def _terminate_processes(timeout_total: float = 2.0):
    for p in list(_processes):
        try:
            if hasattr(p, "is_alive") and p.is_alive():
                p.terminate()
        except Exception:
            pass
    deadline = time.time() + timeout_total
    for p in list(_processes):
        try:
            rem = max(0.0, deadline - time.time())
            if hasattr(p, "join"):
                p.join(timeout=rem / max(1, len(_processes)))
        except Exception:
            pass
    _processes.clear()

def _terminate_popens(timeout_total: float = 2.0):
    for p in list(_popens):
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    deadline = time.time() + timeout_total
    for p in list(_popens):
        try:
            rem = max(0.0, deadline - time.time())
            try:
                p.wait(timeout=rem / max(1, len(_popens)))
            except Exception:
                try: p.kill()
                except Exception: pass
        except Exception:
            pass
    _popens.clear()

def run_cleanup():
    """Cierra recursos de este programa (no toca nada del sistema)."""
    # orden razonable: UI → custom → sockets → subproc → proc → threads
    try: _close_tk_window()
    except Exception: pass
    try: _run_custom_cleanups()
    except Exception: pass
    try: _close_sockets()
    except Exception: pass
    try: _terminate_popens()
    except Exception: pass
    try: _terminate_processes()
    except Exception: pass
    try: _stop_threads()
    except Exception: pass

def hard_exit(code: int = 0):
    """Limpia TODO lo registrado y finalmente mata *este* proceso."""
    try:
        run_cleanup()
    finally:
        os._exit(code)

# ---- Ganchos automáticos ----
atexit.register(run_cleanup)

# Señales típicas: Ctrl+C / terminar servicio
try:
    def _sig_handler(signum, frame):
        hard_exit(0)

    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _sig_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _sig_handler)
    if hasattr(signal, "SIGBREAK"):  # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, _sig_handler)
except Exception:
    pass
