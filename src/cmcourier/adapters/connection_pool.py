"""Pool de conexiones ODBC thread-local con poda de threads muertos (106).

Encapsula el patrón introducido por 095 en :class:`As400NiarvilogStore`
— una conexión pyodbc por thread, cacheada en ``threading.local``, con
un registro global para que ``close_all()`` pueda cerrarlas desde
cualquier thread — y le suma el fix de la fuga 106: los
``ThreadPoolExecutor`` del pipeline se crean y destruyen por chunk, y
las conexiones de los threads muertos quedaban abiertas (jobs
QZDASOINIT acumulándose en el iSeries) hasta el ``close()`` final.

El pool no conoce pyodbc: recibe un ``factory`` que abre la conexión y
traduce errores al tipo de excepción del adapter. Cualquier objeto con
``close()`` sirve como conexión.
"""

from __future__ import annotations

__all__ = ["ThreadLocalConnectionPool"]

import logging
import threading
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)


class ThreadLocalConnectionPool:
    """Una conexión por thread, lazy, con cierre global y poda.

    * ``acquire()`` — devuelve la conexión cacheada del thread actual o
      abre una nueva vía el factory. Al abrir una nueva, cierra y
      desregistra las conexiones cuyos threads ya murieron: el ciclo
      "pool de chunk muere → pool de chunk nuevo conecta" recicla las
      conexiones del chunk anterior.
    * ``reset_current()`` — cierra y desregistra SOLO la conexión del
      thread actual (para retries que necesitan reconectar).
    * ``close_all()`` — cierra todas, desde cualquier thread.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._local = threading.local()
        # Registro (thread, conn): el thread permite detectar entradas
        # huérfanas; ``threading.local`` no expone lo de otros threads.
        self._entries: list[tuple[threading.Thread, Any]] = []
        self._lock = threading.Lock()

    @property
    def open_count(self) -> int:
        """Cantidad de conexiones registradas (vivas o aún no podadas)."""
        with self._lock:
            return len(self._entries)

    def acquire(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = self._factory()
        self._local.conn = conn
        with self._lock:
            self._prune_dead_locked()
            self._entries.append((threading.current_thread(), conn))
        return conn

    def reset_current(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        _close_quietly(conn)
        with self._lock:
            self._entries = [(t, c) for t, c in self._entries if c is not conn]

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries)
            self._entries.clear()
        for _thread, conn in entries:
            _close_quietly(conn)
        self._local = threading.local()

    def _prune_dead_locked(self) -> None:
        """Cierra las conexiones de threads muertos. Llamar con ``_lock``."""
        alive: list[tuple[threading.Thread, Any]] = []
        for thread, conn in self._entries:
            if thread.is_alive():
                alive.append((thread, conn))
            else:
                _close_quietly(conn)
        self._entries = alive


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001 — el cierre nunca propaga
        _log.debug("ODBC connection close failed", exc_info=True)
