"""Clasificador de fallas de upload por tipo (104, REQ-001/002).

Mapea una excepción de S5 a un bucket tipado para el desglose de la tasa
de error que pide el plan de stress (§3.4.2). Cinco buckets:

* ``timeout``    — la request excedió el timeout (ConnectTimeout,
  ReadTimeout, …);
* ``http_4xx``   — el server CMIS rechazó la request (4xx);
* ``http_5xx``   — error del server CMIS (5xx);
* ``transport``  — falla de transporte no-timeout (ConnectError,
  RemoteProtocolError, abort Windows 10053);
* ``app_error``  — cualquier otra cosa (PDF roto, respuesta malformada,
  excepción inesperada).

Módulo puro: importa solo :mod:`cmcourier.domain` y la stdlib. La
distinción timeout/transport sobre excepciones de ``httpx`` se hace por
el **nombre** de la clase, sin importar ``httpx`` — así el clasificador
no acopla la capa de observabilidad a un cliente HTTP concreto.

La función desenvuelve :class:`RetriesExhaustedError` por su ``__cause__``
hasta la causa raíz: un timeout que agota el presupuesto de reintentos
cuenta como ``timeout``, no como una categoría genérica de "reintentos".
"""

from __future__ import annotations

__all__ = ["ErrorCategory", "classify_failure"]

from typing import Literal

from cmcourier.domain.exceptions import (
    CMISClientError,
    CMISServerError,
    RetriesExhaustedError,
)

ErrorCategory = Literal["timeout", "http_4xx", "http_5xx", "transport", "app_error"]


def classify_failure(exc: BaseException) -> tuple[ErrorCategory, int | None]:
    """Clasifica *exc* en ``(categoría, status_code)``.

    ``status_code`` es el código HTTP exacto para ``http_4xx`` /
    ``http_5xx`` (alimenta el sub-desglose por status), o ``None`` para
    el resto de las categorías.
    """
    # REQ-002: desenvolver hasta la causa raíz. El uploader hace
    # ``raise RetriesExhaustedError(...) from last_exc``.
    if isinstance(exc, RetriesExhaustedError) and exc.__cause__ is not None:
        return classify_failure(exc.__cause__)

    if isinstance(exc, CMISClientError):
        return ("http_4xx", exc.status_code)
    if isinstance(exc, CMISServerError):
        return ("http_5xx", exc.status_code)

    # Excepciones de transporte (httpx u otro cliente): clasificamos por
    # el nombre de la clase para no acoplar este módulo a httpx.
    name = type(exc).__name__
    if "Timeout" in name:
        return ("timeout", None)
    if type(exc).__module__.split(".", 1)[0] == "httpx":
        return ("transport", None)

    return ("app_error", None)
