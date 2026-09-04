"""Etapa S5 — :class:`CmisUploader`.

Implementación concreta de :class:`IUploader` para IBM Content Manager vía
el protocolo REST/JSON del CMIS Browser Binding. El adaptador mantiene un
único :class:`httpx.Client` compartido entre todas las llamadas. Cliente
sync; el ``ThreadPoolExecutor`` del orquestador lo invoca desde N `threads`
`worker`.

060: migrado de ``requests`` a ``httpx[http2]``. Cuando el server negocia
HTTP/2 vía `ALPN` (Alfresco con Apache adelante en prod), los N `workers`
concurrentes hacen `multiplexing` sobre una sola conexión TCP — baja el
overhead de los uploads chicos. Si el server solo habla HTTP/1.1 (staging
con Tomcat directo) httpx hace fallback transparente, mismo comportamiento
que pre-060.

Implementa el contrato completo de upload de S5:

* Warmup de JSESSIONID — lazy, corre una vez por session lifetime.
* Verificación read-only de carpetas (``verify_folder_exists``, la usa
  el doctor) y recuperación idempotente de 409: si el documento ya
  existe en la carpeta destino, se busca su objectId en vez de fallar.
* Upload `multipart` `streaming` vía ``MultipartEncoder`` +
  ``content=`` (076); el archivo se lee de disco bajo demanda, nunca se
  bufferea entero.
* :class:`BandwidthLimiter` opcional que envuelve el `stream` del archivo
  para redes corporativas con throttling.
* Política de `retry`: 401 → re-warmup + un `retry`; 5xx → `back-off`
  exponencial capado a 60 s; aborto de conexión Windows-10053 → sleep
  duplicado; 4xx → fail-fast :class:`CMISClientError`; presupuesto de
  `retry` agotado → :class:`RetriesExhaustedError`.
* El parser de ``cmis:objectId`` de 3 rutas.

Principio I de la Constitución: este módulo importa ``httpx`` (declarado en
``pyproject.toml``) y la standard library. Los modelos de dominio se
importan solo como tipos. Principio VIII: los logs identifican claves
operacionales (txn_num, folder_path, status HTTP, attempt) pero nunca
valores de propiedades ni cuerpos de respuesta más allá de un cap de
truncado.
"""

from __future__ import annotations

__all__ = ["BandwidthLimiter", "CmisConfig", "CmisUploader", "TokenBucket"]

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import IO, Any

import httpx
from requests_toolbelt import (  # 076 + 077: streaming multipart + progress
    MultipartEncoder,
    MultipartEncoderMonitor,
)

from cmcourier.domain.exceptions import (
    CMISClientError,
    CMISServerError,
    RetriesExhaustedError,
)
from cmcourier.domain.models import StagedFile
from cmcourier.domain.ports import IUploader
from cmcourier.observability.pii import mask_dict

_network_log = logging.getLogger("cmcourier.metrics.network")

_log = logging.getLogger(__name__)

_WINDOWS_ABORT_MARKER = "10053"
_MAX_BACKOFF_S = 60.0
_RESPONSE_BODY_TRUNCATION = 1024
# 077: threshold mínimo de bytes acumulados antes de emitir un
# ``cmis_upload_progress``. 111: subido de 1 MiB a 8 MiB — cada evento
# paga json.dumps + escritura a disco + dos handlers desde el worker
# thread de S5, y el chart de bandwidth (60 buckets de 1 s) no necesita
# granularidad más fina; el completion acredita el remanente igual.
_PROGRESS_THRESHOLD_BYTES = 8 * 1_048_576
# 116: techo del timeout de CONNECT. Pre-116 ``httpx.Timeout(300)``
# uniforme hacía que un host caído tardara 5 minutos en fallar el
# handshake; read/write conservan el timeout configurado (los uploads
# grandes sobre redes lentas sí lo necesitan).
_CONNECT_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CmisConfig:
    """Perillas de conexión + `retry` + ancho de banda para :class:`CmisUploader`."""

    base_url: str
    repo_id: str
    username: str
    password: str
    timeout_seconds: float = 300.0
    verify_ssl: bool = False
    max_bandwidth_mbps: float = 0.0
    retry_max_attempts: int = 3
    retry_base_delay_s: float = 2.0
    # 038: el `connection pool` default de httpx es chico. Cuando la
    # cantidad de `workers` de S5 supera el pool `keep-alive`, httpx abre
    # TCP fresco por request. Dimensionamos el pool para que coincida con
    # la concurrencia esperada.
    pool_size: int = 10
    # 038: cuando es True, los eventos ``s5_upload_attempt`` y
    # ``s5_upload_failed`` emiten valores de propiedad crudos en lugar de
    # enmascarados como PII. Se togglea vía ``ObservabilityConfig.unmask_pii``;
    # nunca default-true.
    unmask_pii: bool = False
    # 089: opt-out de HTTP/2. Default True preserva el comportamiento
    # de spec 060 (negocia h2 vía ALPN, fallback a 1.1). Setearlo
    # False fuerza HTTP/1.1 — cada worker mantiene su propia
    # conexión TCP (hasta ``pool_size`` keepalive). Útil cuando
    # multiplexing HTTP/2 sobre pocas conexiones físicas serializa
    # el throughput agregado de uploads grandes paralelos.
    http2: bool = True
    # 090: chunk size de lectura del MultipartEncoder. Pre-090
    # ``enc.read(8192)`` hardcoded — con N workers paralelos el
    # GIL serializaba los reads (cada chunk dispara CPU work en
    # Python: format multipart, copy buffers, progress callback).
    # Default 1 MiB minimiza GIL acquisitions y restaura el
    # paralelismo real.
    upload_chunk_bytes: int = 1 << 20


# ---------------------------------------------------------------------------
# BandwidthLimiter
# ---------------------------------------------------------------------------


class TokenBucket:
    """`token bucket` compartido a nivel de proceso (corregido en 029).

    :class:`CmisUploader` es dueño de una sola instancia y se reusa en
    cada upload + cada `thread` `worker`. Las llamadas concurrentes a
    ``consume()`` se serializan sobre el `lock` interno, así que la tasa
    configurada es el techo **global**, no uno por llamada. ``mbps=0``
    desactiva el throttling por completo (no se toma el `lock`).
    """

    def __init__(
        self,
        mbps: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # 081: ``mbps`` se interpreta como **megabits per second**
        # (convención estándar de networking — el nombre del field
        # ``cmis.max_bandwidth_mbps`` lo dice explícitamente). Convertimos
        # a bytes/seg dividiendo por 8: 1 Mbps = 1_000_000 bits/s =
        # 125_000 bytes/s. Pre-081 el código trataba el valor como MB/s
        # (megabytes/s) por error — un valor de "50" significaba 50 MB/s
        # = 400 Mbps, 8x más permisivo de lo que el operador pedía.
        self._enabled = mbps > 0
        self._rate = mbps * 125_000.0  # Mbps → bytes/seg
        # 116: cap de burst de 1 segundo de presupuesto. Pre-116 los
        # tokens se acumulaban sin techo durante las pausas (fase de
        # PREP larga, hueco entre chunks) y la primera ráfaga salía sin
        # throttling — el límite se violaba justo cuando más importa.
        self._capacity = self._rate * 1.0
        self._tokens = 0.0
        self._clock = clock
        self._sleep = sleep
        self._last_refill = clock()
        self._lock = threading.Lock()

    def consume(self, n_bytes: int) -> None:
        """Bloquea hasta haber pagado ``n_bytes`` tokens, drenando en cuotas.

        116: con el cap de burst, un pedido mayor que ``_capacity`` no
        puede satisfacerse de una — se consume lo disponible y se
        duerme por el resto, en cuotas de a lo sumo un cap. La tasa
        total resultante es exactamente ``_rate``.
        """
        if not self._enabled or n_bytes <= 0:
            return
        remaining = float(n_bytes)
        # Computamos el sleep fuera del `lock` para que otros `threads` puedan
        # refrescar su matemática de tokens mientras este espera. El `lock`
        # solo protege el estado (tokens, last_refill).
        while True:
            with self._lock:
                now = self._clock()
                elapsed = now - self._last_refill
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last_refill = now
                take = min(self._tokens, remaining)
                self._tokens -= take
                remaining -= take
                # Tolerancia sub-byte: el residuo de float (p. ej. 5e-14
                # bytes) no debe forzar otra vuelta — con sleeps
                # infinitesimales el refill no progresa y el loop gira.
                if remaining < 1.0:
                    return
                # Piso de 1 ms: dormir menos que la granularidad del
                # scheduler es puro spin.
                deficit = max(min(remaining, self._capacity) / self._rate, 0.001)
            self._sleep(deficit)


class BandwidthLimiter:
    """Wrapper file-like que delega el throttling a un `bucket` compartido."""

    def __init__(self, stream: IO[bytes], bucket: TokenBucket) -> None:
        self._stream = stream
        self._bucket = bucket

    def read(self, size: int = -1) -> bytes:
        # 116: leer primero y cobrar por los bytes REALES — pre-116 se
        # consumían ``chunk_size`` tokens antes de leer, y las lecturas
        # cortas / el EOF pagaban de más (throughput real por debajo del
        # configurado). El pacing sigue ocurriendo antes de devolver el
        # chunk al encoder, así que el throttle es equivalente.
        chunk_size = size if size >= 0 else 1 << 20
        data = self._stream.read(chunk_size)
        self._bucket.consume(len(data))
        return data

    def seek(self, *args: Any, **kwargs: Any) -> int:
        return self._stream.seek(*args, **kwargs)

    def tell(self) -> int:
        return self._stream.tell()

    def close(self) -> None:
        self._stream.close()

    def fileno(self) -> int:
        """Delega al file handle subyacente.

        080: ``requests_toolbelt.total_len()`` prueba ``fileno()`` para
        medir el tamaño del file part al armar ``MultipartEncoder``.
        Sin este método, ``total_len`` devuelve ``None`` silenciosamente
        y el encoder tira ``TypeError: unsupported operand +: int + None``
        al calcular ``Content-Length`` en su ``__init__``. Ese bug bloqueaba
        100% de los uploads cuando ``cmis.max_bandwidth_mbps > 0`` (el
        path donde el stream se envuelve en ``BandwidthLimiter``).
        """
        return self._stream.fileno()

    @property
    def name(self) -> str:
        return str(getattr(self._stream, "name", "<bandwidth-limited>"))

    def __enter__(self) -> BandwidthLimiter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# CmisUploader
# ---------------------------------------------------------------------------


class CmisUploader(IUploader):
    """Implementación concreta de :class:`IUploader` sobre CMIS Browser Binding (httpx[http2])."""

    def __init__(self, config: CmisConfig) -> None:
        self._cfg = config
        pool_size = max(1, int(config.pool_size))
        # 060: HTTP/2 negociado vía `ALPN`; fallback a HTTP/1.1 si el server
        # no anuncia `h2`. Mismo comportamiento de cable que pre-060 contra
        # Tomcat directo; el `multiplexing` aparece contra Apache.
        self._client = httpx.Client(
            http2=config.http2,
            auth=(config.username, config.password),
            verify=config.verify_ssl,
            limits=httpx.Limits(
                max_connections=pool_size,
                max_keepalive_connections=pool_size,
            ),
            # 116: connect capeado — read/write/pool con el valor
            # configurado.
            timeout=httpx.Timeout(
                config.timeout_seconds,
                connect=min(_CONNECT_TIMEOUT_S, float(config.timeout_seconds)),
            ),
        )
        self._warm = False
        # 025: el `worker pool` de S5 invoca upload concurrentemente. El
        # estado por instancia de arriba se comparte entre `threads` `worker`.
        self._warm_lock = threading.Lock()
        # 025 fase 2: el controlador AIMD de auto-tune puede ajustar el
        # timeout de request a mitad de `batch`. CmisConfig en sí está
        # frozen, así que guardamos el valor en vivo acá. Los caminos de
        # request consultan esta propiedad vía ``self._timeout_s``; por
        # defecto, el valor configurado.
        self._timeout_s: float = float(config.timeout_seconds)
        # 029: un único TokenBucket compartido por uploader para que el
        # ``max_bandwidth_mbps`` configurado sea el cap global entre todos
        # los uploads concurrentes (no un techo por llamada que se
        # multiplica por la cantidad de `workers`).
        self._bandwidth_bucket = TokenBucket(mbps=config.max_bandwidth_mbps)

    @property
    def current_timeout_s(self) -> float:
        """122: el timeout vivo (el AIMD lo ajusta a mitad de batch).
        Accessor público para el TUI — pre-122 el data provider leía
        ``_timeout_s`` directo."""
        return self._timeout_s

    def _request_timeout(self) -> httpx.Timeout:
        """116: timeout por request con connect capeado.

        ``_timeout_s`` es el valor vivo que el AIMD ajusta a mitad de
        batch — aplica a read/write/pool; el connect queda corto para
        que un host caído falle en segundos, no en minutos.
        """
        return httpx.Timeout(
            self._timeout_s,
            connect=min(_CONNECT_TIMEOUT_S, self._timeout_s),
        )

    # ----------------------------------------------------------- API pública

    def test_connection(self) -> Mapping[str, str]:
        """GET repositoryInfo, devuelve un dict chico de diagnóstico.

        IBM CM devuelve los campos planos bajo el objeto JSON de nivel
        superior. Alfresco los envuelve: ``{"<repo_id>": {"repositoryId": ...}}``.
        Desenvolvemos cuando al nivel superior le falta ``repositoryId`` (040).
        """
        data = self._warmup_session()
        if isinstance(data, dict) and "repositoryId" not in data:
            nested = [v for v in data.values() if isinstance(v, dict) and "repositoryId" in v]
            if nested:
                data = nested[0]
        return {
            "repository_id": str(data.get("repositoryId", "")),
            "product_name": str(data.get("productName", "")),
            "product_version": str(data.get("productVersion", "")),
            "vendor_name": str(data.get("vendorName", "")),
        }

    def warm_connection_pool(self, n: int) -> int:
        """Pre-abre ``n`` conexiones TCP+`TLS`+JSESSIONID (038).

        Sin esto, los primeros ``n`` uploads de S5 pagan cada uno el
        handshake TCP + `TLS` + bootstrap de `JSESSIONID` en la ruta
        crítica — fácil 100-400 ms por `worker` en un enlace corporativo.
        Llamar a esto una vez antes de S5 manda todo ese costo a una fase
        de arranque paralela, dejando los uploads en sí sobre conexiones
        `keep-alive` calientes.

        Devuelve la cantidad de warmups que completaron exitosamente. Las
        fallas se loguean pero nunca se levantan — un pool frío solo
        implica que los primeros uploads pagan el handshake.
        """
        if n <= 0:
            return 0
        successes = 0
        with ThreadPoolExecutor(
            max_workers=n,
            thread_name_prefix="cmcourier-cmis-warmup",
        ) as pool:
            futures = [pool.submit(self._warmup_session) for _ in range(n)]
            for fut in as_completed(futures):
                try:
                    fut.result()
                    successes += 1
                except (CMISServerError, CMISClientError, httpx.RequestError):
                    _log.warning("cmis: warmup attempt failed", exc_info=True)
        _log.info(
            "cmis: connection pool warmed",
            extra={
                "event": "cmis_pool_warmed",
                "requested": n,
                "succeeded": successes,
            },
        )
        return successes

    def get_type_definition(self, object_type_id: str) -> Mapping[str, Any]:
        """GET cmisselector=typeDefinition&typeId=<id>. No pasa por el loop de `retry`."""
        with self._warm_lock:
            need_warmup = not self._warm
        if need_warmup:
            self._warmup_session()
        url = self._service_url()
        t0 = time.monotonic()
        resp = self._client.get(
            url,
            params={"cmisselector": "typeDefinition", "typeId": object_type_id},
            timeout=self._request_timeout(),
        )
        _network_log.info(
            "cmis_get",
            extra={
                "kind": "cmis_get",
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                "status": resp.status_code,
                "url_prefix": url[:80],
                "worker": threading.current_thread().name,
            },
        )
        if resp.status_code >= 400:
            raise _http_error(resp)
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def verify_folder_exists(self, folder_path: str) -> bool:
        """Devuelve ``True`` si y solo si *folder_path* existe en el server CM
        y es un ``cmis:folder``.

        Read-only — nunca crea la carpeta. CMCourier solo deposita
        documentos; el árbol de carpetas destino lo gobierna el
        administrador `cmis` (038). Lo usa ``doctor --check cm-targets``
        para hacer `pre-flight` de cada ``CMISFolder`` declarado en
        MapeoRVI_CM antes de que S5 corra.

        Devuelve ``False`` ante un 404 o cuando la ruta resuelve a un
        objeto que no es carpeta. Levanta ``CMISClientError`` (401/403) o
        ``CMISServerError`` (5xx) ante fallas de conectividad /
        autenticación para que doctor exponga los errores de configuración
        en voz alta.
        """
        normalized = folder_path.strip("/")
        url = self._service_url(f"root/{normalized}")
        t0 = time.monotonic()
        resp = self._client.get(
            url,
            params={"cmisselector": "object"},
            timeout=self._request_timeout(),
        )
        _network_log.info(
            "cmis_get",
            extra={
                "kind": "cmis_verify_folder",
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                "status": resp.status_code,
                "url_prefix": url[:80],
                "worker": threading.current_thread().name,
            },
        )
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise _http_error(resp)
        try:
            data = resp.json()
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
        props = data.get("properties") or data.get("succinctProperties") or {}
        if not isinstance(props, dict):
            return False
        base = props.get("cmis:baseTypeId")
        if isinstance(base, dict):
            base = base.get("value")
        return base == "cmis:folder"

    def upload(
        self,
        file: StagedFile,
        folder_path: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
        *,
        batch_id: str,
    ) -> str:
        """`stream`ea el archivo staged y devuelve el cmis:objectId resultante.

        ``batch_id`` etiqueta cada evento de red que se emite acá para que
        el ``_BandwidthHandler`` / ``_SlowOpHandler`` por `batch` atribuyan
        los bytes + slow ops al `chunk` correcto. Sin él, los handlers
        descartan los eventos (su filtro de ``batch_id`` nunca matchea).

        La carpeta destino NO se verifica ni se crea acá — se espera que el
        operador haya corrido ``doctor --check cm-targets`` (038) antes del
        `pipeline`. Si la carpeta no existe o no es una carpeta `cmis`, el
        server devuelve un 4xx y la falla aparece vía el camino existente
        de `retry` / métricas.
        """
        normalized = folder_path.strip("/")
        url = self._service_url(f"root/{normalized}")
        self._emit_upload_attempt(
            url=url,
            object_type_id=object_type_id,
            document_name=document_name,
            mime_type=mime_type,
            properties=properties,
            content_bytes=file.size_bytes,
            batch_id=batch_id,
        )
        with file.path.open("rb") as fh:
            stream: IO[bytes] = (
                BandwidthLimiter(fh, self._bandwidth_bucket)  # type: ignore[assignment]
                if self._cfg.max_bandwidth_mbps > 0
                else fh
            )
            data_fields, file_field = self._build_multipart_for_upload(
                stream, document_name, mime_type, object_type_id, properties
            )
            try:
                resp = self._post_with_retries(
                    url,
                    data_fields=data_fields,
                    file_field=file_field,
                    txn_num=document_name,
                    kind="cmis_upload",
                    size_bytes=file.size_bytes,
                    batch_id=batch_id,
                )
            except (CMISClientError, CMISServerError) as exc:
                # 045: un conflicto 409 típicamente significa que una
                # corrida anterior (o una `race condition` entre un POST
                # `cmis` exitoso y nuestro commit de SQLite) ya creó este
                # objeto. Lo buscamos por cmis:name; si aparece, tratamos
                # el upload como exitoso de forma `idempotent` y
                # devolvemos ese objectId.
                if isinstance(exc, CMISClientError) and exc.status_code == 409:
                    recovered = self._try_recover_409(
                        folder_url=url,
                        document_name=document_name,
                        object_type_id=object_type_id,
                        mime_type=mime_type,
                        properties=properties,
                        content_bytes=file.size_bytes,
                        exc=exc,
                    )
                    if recovered is not None:
                        return recovered
                self._emit_upload_failed(
                    url=url,
                    object_type_id=object_type_id,
                    document_name=document_name,
                    mime_type=mime_type,
                    properties=properties,
                    content_bytes=file.size_bytes,
                    batch_id=batch_id,
                    status_code=exc.status_code,
                    response_body=str(getattr(exc, "response_body", "") or "")[
                        :_RESPONSE_BODY_TRUNCATION
                    ],
                )
                raise
        return self._parse_object_id(resp)

    def _try_recover_409(
        self,
        *,
        folder_url: str,
        document_name: str,
        object_type_id: str,
        mime_type: str,
        properties: Mapping[str, str],
        content_bytes: int,
        exc: CMISClientError,
    ) -> str | None:
        """045 — recupera un conflicto 409 vía lookup de hijos.

        Devuelve el ``cmis:objectId`` existente cuando un hijo de
        ``folder_url`` ya tiene ``cmis:name == document_name``; devuelve
        ``None`` cuando no matchea nada (409 real — propagamos como
        falla). Emite eventos de auditoría estructurados para que el
        operador pueda grepear las recuperaciones en
        ``network-YYYY-MM-DD.jsonl``.
        """
        self._emit_409_event(
            event="s5_upload_409_recovery_attempt",
            url=folder_url,
            object_type_id=object_type_id,
            document_name=document_name,
            mime_type=mime_type,
            properties=properties,
            content_bytes=content_bytes,
        )
        try:
            existing_id = self._lookup_existing_object_id(folder_url, document_name)
        except (CMISClientError, CMISServerError, httpx.RequestError):
            # El lookup en sí falló — caemos al camino original de falla
            # para que el operador vea el error subyacente del upload.
            self._emit_409_event(
                event="s5_upload_409_recovery_failed",
                url=folder_url,
                object_type_id=object_type_id,
                document_name=document_name,
                mime_type=mime_type,
                properties=properties,
                content_bytes=content_bytes,
                detail="lookup-transport-error",
            )
            return None
        if existing_id is None:
            self._emit_409_event(
                event="s5_upload_409_recovery_failed",
                url=folder_url,
                object_type_id=object_type_id,
                document_name=document_name,
                mime_type=mime_type,
                properties=properties,
                content_bytes=content_bytes,
                detail="no-matching-child",
            )
            return None
        self._emit_409_event(
            event="s5_upload_409_recovered",
            url=folder_url,
            object_type_id=object_type_id,
            document_name=document_name,
            mime_type=mime_type,
            properties=properties,
            content_bytes=content_bytes,
            recovered_object_id=existing_id,
        )
        del exc  # el 409 original se consume — la recuperación es la respuesta ahora.
        return existing_id

    def _lookup_existing_object_id(self, folder_url: str, document_name: str) -> str | None:
        """Lista los hijos de ``folder_url``, devuelve el cmis:objectId que matchee."""
        t0 = time.monotonic()
        # 120: solo las dos propiedades que el matching necesita, en
        # formato succinct — pre-120 cada hijo viajaba con TODAS sus
        # propiedades CMIS (respuesta enorme por cada 409 en carpetas
        # grandes).
        resp = self._client.get(
            folder_url,
            params={
                "cmisselector": "children",
                "maxItems": "5000",
                "filter": "cmis:name,cmis:objectId",
                "succinct": "true",
            },
            timeout=self._request_timeout(),
        )
        _network_log.info(
            "cmis_get",
            extra={
                "kind": "cmis_409_lookup",
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                "status": resp.status_code,
                "url_prefix": folder_url[:80],
                "worker": threading.current_thread().name,
            },
        )
        if resp.status_code >= 400:
            raise _http_error(resp)
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        objects = data.get("objects") or []
        if not isinstance(objects, list):
            return None
        for entry in objects:
            obj = entry.get("object") if isinstance(entry, dict) else None
            if not isinstance(obj, dict):
                continue
            props = obj.get("properties") or obj.get("succinctProperties") or {}
            if not isinstance(props, dict):
                continue
            name = props.get("cmis:name")
            if isinstance(name, dict):
                name = name.get("value")
            if name != document_name:
                continue
            obj_id = props.get("cmis:objectId")
            if isinstance(obj_id, dict):
                obj_id = obj_id.get("value")
            if isinstance(obj_id, str) and obj_id:
                return obj_id
        return None

    def _emit_409_event(
        self,
        *,
        event: str,
        url: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
        content_bytes: int,
        recovered_object_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        masked = mask_dict(dict(properties), unmask=self._cfg.unmask_pii)
        extra: dict[str, Any] = {
            "event": event,
            "kind": "cmis_409_recovery",
            "url": url,
            "object_type_id": object_type_id,
            "document_name": document_name,
            "mime_type": mime_type,
            "content_bytes": content_bytes,
            "properties_json": json.dumps(masked, sort_keys=True),
        }
        if recovered_object_id is not None:
            extra["recovered_object_id"] = recovered_object_id
        if detail is not None:
            extra["detail"] = detail
        _network_log.info(event, extra=extra)

    def _emit_upload_attempt(
        self,
        *,
        url: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
        content_bytes: int,
        batch_id: str,
    ) -> None:
        """038: evento ``s5_upload_attempt`` estructurado hacia metrics.jsonl."""
        masked = mask_dict(
            {
                "cmis:name": document_name,
                "cmis:contentStreamMimeType": mime_type,
                **dict(properties),
            },
            unmask=self._cfg.unmask_pii,
        )
        _network_log.info(
            "s5_upload_attempt",
            extra={
                "event": "s5_upload_attempt",
                "kind": "cmis_upload_attempt",
                "batch_id": batch_id,
                "url": url[:200],
                "object_type_id": object_type_id,
                "document_name": document_name,
                "mime_type": mime_type,
                "properties_json": json.dumps(masked, ensure_ascii=False, sort_keys=True),
                "content_bytes": content_bytes,
                "worker": threading.current_thread().name,
            },
        )

    def _emit_upload_failed(
        self,
        *,
        url: str,
        object_type_id: str,
        document_name: str,
        mime_type: str,
        properties: Mapping[str, str],
        content_bytes: int,
        batch_id: str,
        status_code: int,
        response_body: str,
    ) -> None:
        """038: evento ``s5_upload_failed`` estructurado con su equivalente curl."""
        masked = mask_dict(
            {
                "cmis:name": document_name,
                "cmis:contentStreamMimeType": mime_type,
                **dict(properties),
            },
            unmask=self._cfg.unmask_pii,
        )
        curl = self._build_curl_equivalent(
            url=url, object_type_id=object_type_id, masked_properties=masked
        )
        _network_log.info(
            "s5_upload_failed",
            extra={
                "event": "s5_upload_failed",
                "kind": "cmis_upload_failed",
                "batch_id": batch_id,
                "url": url[:200],
                "object_type_id": object_type_id,
                "document_name": document_name,
                "mime_type": mime_type,
                "properties_json": json.dumps(masked, ensure_ascii=False, sort_keys=True),
                "content_bytes": content_bytes,
                "status_code": status_code,
                "response_body": response_body,
                "curl_equivalent": curl,
                "worker": threading.current_thread().name,
            },
        )

    def _build_curl_equivalent(
        self, *, url: str, object_type_id: str, masked_properties: Mapping[str, str]
    ) -> str:
        """Renderiza un curl ejecutable que reproduce el POST que falló.

        El `auth` se renderiza como ``-u admin:***`` sin importar
        unmask_pii — las credenciales nunca se filtran a los logs
        estructurados (Principio VIII).
        """
        parts = [
            "curl -u admin:***",
            "-X POST",
            "-F 'cmisaction=createDocument'",
            f"-F 'propertyId[0]=cmis:objectTypeId' -F 'propertyValue[0]={object_type_id}'",
        ]
        idx = 1
        for k, v in masked_properties.items():
            if k in ("cmis:contentStreamMimeType",):
                # ya está en el form; salteamos el duplicado (el encoder
                # siempre lleva el mime vía `cmis:contentStreamMimeType`).
                pass
            safe_v = v.replace("'", "'\\''")
            parts.append(f"-F 'propertyId[{idx}]={k}' -F 'propertyValue[{idx}]={safe_v}'")
            idx += 1
        parts.append("-F 'content=@<staged_pdf_path>'")
        parts.append(f"'{url}'")
        return " ".join(parts)

    # ----------------------------------------------------------- internos

    def _service_url(self, suffix: str = "") -> str:
        """Construye una URL de servicio CMIS Browser Binding respetando Alfresco vs IBM CM.

        IBM Content Manager expone su `repo` id DENTRO del path de la URL
        (``.../cmis-browser/<repo_id>/root/<folder>``). El browser binding
        de Alfresco NO lo hace — el `repo` id se lee de la respuesta de
        ``repositoryInfo``, y la ``base_url`` ya incluye todo hasta
        ``.../browser`` (040). Los distinguimos vía la config ``repo_id``:

        - ``repo_id`` seteado (cualquier string no vacío): convención IBM CM,
          emite ``f"{base}/{repo_id}/{suffix}"``.
        - ``repo_id == ""``: convención Alfresco, emite
          ``f"{base}/{suffix}"`` (sin slash duplicada).
        """
        if self._cfg.repo_id:
            url = f"{self._cfg.base_url}/{self._cfg.repo_id}"
        else:
            url = self._cfg.base_url
        return f"{url}/{suffix}" if suffix else url

    def _warmup_session(self) -> dict[str, Any]:
        url = self._service_url()
        t0 = time.monotonic()
        resp = self._client.get(
            url,
            params={"cmisselector": "repositoryInfo"},
            timeout=self._request_timeout(),
        )
        _network_log.info(
            "cmis_get",
            extra={
                "kind": "cmis_get",
                "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
                "status": resp.status_code,
                "url_prefix": url[:80],
                "worker": threading.current_thread().name,
            },
        )
        body = _truncate(resp.text)
        if resp.status_code >= 500:
            raise CMISServerError(status_code=resp.status_code, response_body=body)
        if resp.status_code >= 400:
            raise CMISClientError(status_code=resp.status_code, response_body=body)
        with self._warm_lock:
            self._warm = True
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def _post_with_retries(
        self,
        url: str,
        *,
        data_fields: dict[str, str],
        file_field: tuple[str, IO[bytes], str],
        txn_num: str,
        kind: str = "cmis_post",
        size_bytes: int | None = None,
        batch_id: str,
    ) -> httpx.Response:
        auth_retried = False
        last_exc: Exception | None = None
        real_attempts = 0
        t0 = time.monotonic()
        # 060: el `stream` del archivo pudo haber sido consumido por un
        # intento previo (httpx lo lee entero en el POST). Hacemos seek a 0
        # antes de cada `retry` — pero solo al file handle subyacente, ya
        # que BandwidthLimiter le reenvía el seek().
        # 105: el rebobinado debe cubrir también el reintento por 401, que
        # no incrementa `real_attempts` — sin él, el file part del segundo
        # POST sale vacío con Content-Length completo y el server espera
        # el resto del body hasta el read timeout.
        stream = file_field[1]
        # 077: contador de bytes reportados como progress events parciales.
        # Vive afuera del loop así está accesible en el branch de
        # retries-exhausted al final. Cada attempt lo resetea a 0.
        reported_state: dict[str, int] = {"bytes": 0}
        while real_attempts < self._cfg.retry_max_attempts:
            with self._warm_lock:
                need_warmup = not self._warm
            if need_warmup:
                self._warmup_session()
            try:
                if real_attempts > 0 or auth_retried:
                    stream.seek(0)
                # 076: `MultipartEncoder` arma el body como un iterator
                # lazy — los chunks van del disco directo al socket TCP
                # sin buffearse el body entero en RAM.
                # 077: lo envolvemos en ``MultipartEncoderMonitor`` para
                # que un callback nos avise cuánto se transmitió cada
                # chunk; emitimos eventos ``cmis_upload_progress`` cuando
                # acumulamos ``_PROGRESS_THRESHOLD_BYTES``, así el TUI
                # ve el throughput en vivo durante uploads largos en
                # lugar de tener que esperar al completion.
                encoder = MultipartEncoder(fields={**data_fields, "content": file_field})
                # 077: reseteamos el contador por cada attempt — el upload
                # arranca de cero después del ``stream.seek(0)``.
                reported_state["bytes"] = 0

                def _progress_callback(
                    monitor: MultipartEncoderMonitor,
                    state: dict[str, int] = reported_state,
                    tx: str = txn_num,
                    bid: str = batch_id,
                ) -> None:
                    current = int(monitor.bytes_read)
                    delta = current - state["bytes"]
                    if delta < _PROGRESS_THRESHOLD_BYTES:
                        return
                    state["bytes"] = current
                    _network_log.info(
                        "cmis_upload_progress",
                        extra={
                            "kind": "cmis_upload_progress",
                            "batch_id": bid,
                            "txn_num": tx,
                            "bytes_delta": delta,
                        },
                    )

                monitored = MultipartEncoderMonitor(encoder, _progress_callback)

                chunk_bytes = self._cfg.upload_chunk_bytes

                def _read_chunk(
                    enc: MultipartEncoderMonitor = monitored,
                    size: int = chunk_bytes,
                ) -> bytes:
                    """090: chunk size configurable (default 1 MiB).
                    ``enc`` y ``size`` se bindean como default-args para
                    capturar el valor de esta iteration (B023).
                    """
                    return bytes(enc.read(size))

                resp = self._client.post(
                    url,
                    content=iter(_read_chunk, b""),
                    headers={
                        "Content-Type": monitored.content_type,
                        "Content-Length": str(monitored.len),
                    },
                    timeout=self._request_timeout(),
                )
            except httpx.RequestError as exc:
                # httpx.RequestError cubre ConnectError, ReadError,
                # RemoteProtocolError, TimeoutException — todas las fallas
                # de transporte que antes hubieran aparecido como
                # requests.exceptions.ConnectionError.
                real_attempts += 1
                last_exc = exc
                doubled = _WINDOWS_ABORT_MARKER in str(exc)
                if doubled:
                    _log.error(
                        "cmis: windows abort 10053",
                        extra={"txn_num": txn_num, "attempt": real_attempts},
                    )
                if real_attempts < self._cfg.retry_max_attempts or doubled:
                    self._backoff_sleep(real_attempts, doubled)
                continue
            if resp.status_code == 401 and not auth_retried:
                auth_retried = True
                with self._warm_lock:
                    self._warm = False
                continue
            if 200 <= resp.status_code < 400:
                self._emit_network(
                    kind,
                    t0,
                    resp.status_code,
                    size_bytes,
                    url,
                    batch_id,
                    progress_bytes=reported_state["bytes"],
                )
                return resp
            if 400 <= resp.status_code < 500:
                self._emit_network(
                    kind,
                    t0,
                    resp.status_code,
                    size_bytes,
                    url,
                    batch_id,
                    progress_bytes=reported_state["bytes"],
                )
                raise CMISClientError(
                    status_code=resp.status_code, response_body=_truncate(resp.text)
                )
            real_attempts += 1
            last_exc = CMISServerError(
                status_code=resp.status_code, response_body=_truncate(resp.text)
            )
            _log.info(
                "cmis: server error, retrying",
                extra={
                    "txn_num": txn_num,
                    "attempt": real_attempts,
                    "status": resp.status_code,
                },
            )
            if real_attempts < self._cfg.retry_max_attempts:
                self._backoff_sleep(real_attempts, doubled=False)
        assert last_exc is not None
        last_status = getattr(last_exc, "status_code", None)
        self._emit_network(
            kind,
            t0,
            last_status,
            size_bytes,
            url,
            batch_id,
            progress_bytes=reported_state["bytes"],
        )
        raise RetriesExhaustedError(
            txn_num=txn_num, attempts=self._cfg.retry_max_attempts
        ) from last_exc

    @staticmethod
    def _emit_network(
        kind: str,
        t0: float,
        status: int | None,
        size_bytes: int | None,
        url: str,
        batch_id: str,
        progress_bytes: int = 0,
    ) -> None:
        # ``batch_id`` es obligatorio: los _BandwidthHandler /
        # _SlowOpHandler por `batch` descartan cualquier record cuyo
        # batch_id no matchee, así que un evento sin él lo descarta en
        # silencio cada recorder.
        extra: dict[str, object] = {
            "kind": kind,
            "batch_id": batch_id,
            "duration_ms": round((time.monotonic() - t0) * 1000.0, 3),
            "url_prefix": url[:80],
            "worker": threading.current_thread().name,
        }
        if status is not None:
            extra["status"] = status
        if size_bytes is not None:
            extra["size_bytes"] = size_bytes
        # 077: cuántos bytes del upload se reportaron como progress
        # events parciales. ``_BandwidthHandler`` resta esto del
        # ``size_bytes`` para evitar double-counting.
        if progress_bytes:
            extra["progress_bytes"] = progress_bytes
        _network_log.info(kind, extra=extra)

    def _backoff_sleep(self, attempt: int, doubled: bool) -> None:
        delay = self._cfg.retry_base_delay_s * (2 ** (attempt - 1))
        if doubled:
            delay *= 2
        time.sleep(min(delay, _MAX_BACKOFF_S))

    def _build_multipart_for_upload(
        self,
        stream: IO[bytes],
        document_name: str,
        mime_type: str,
        object_type_id: str,
        properties: Mapping[str, str],
    ) -> tuple[dict[str, str], tuple[str, IO[bytes], str]]:
        """Construye el cuerpo `multipart` para `client.post(..., data=..., files=...)`.

        060: httpx separa los campos de texto (``data``) de los campos de
        archivo (``files``). Devuelve ``(data_fields, file_field)`` para
        que el caller arme el POST. Los campos de texto llevan todas las
        propiedades `cmis` (``cmisaction``, ``propertyId[N]`` /
        ``propertyValue[N]``) y el campo de archivo lleva el `stream`
        staged.

        040: IBM CM requiere ``cmis:contentStreamMimeType`` como
        propiedad explícita; Alfresco rechaza esa misma propiedad con 400
        porque infiere el mime type del Content-Type del `multipart`. La
        convención sigue la misma heurística Alfresco-vs-IBM-CM que el
        URL builder: ``repo_id`` vacío significa modo Alfresco → omitimos
        la propiedad explícita; la parte del `multipart` igual lleva el
        Content-Type correcto.
        """
        data_fields: dict[str, str] = {
            "cmisaction": "createDocument",
            "propertyId[0]": "cmis:objectTypeId",
            "propertyValue[0]": object_type_id,
            "propertyId[1]": "cmis:name",
            "propertyValue[1]": document_name,
        }
        next_idx = 2
        if self._cfg.repo_id:
            data_fields[f"propertyId[{next_idx}]"] = "cmis:contentStreamMimeType"
            data_fields[f"propertyValue[{next_idx}]"] = mime_type
            next_idx += 1
        for i, (key, value) in enumerate(properties.items()):
            data_fields[f"propertyId[{next_idx + i}]"] = key
            data_fields[f"propertyValue[{next_idx + i}]"] = value
        file_field = (document_name, stream, mime_type)
        return data_fields, file_field

    @staticmethod
    def _parse_object_id(response: httpx.Response) -> str:
        try:
            data = response.json()
        except ValueError:
            return "unknown"
        if isinstance(data, dict):
            succinct = data.get("succinctProperties")
            if isinstance(succinct, dict) and "cmis:objectId" in succinct:
                return str(succinct["cmis:objectId"])
            properties = data.get("properties")
            if isinstance(properties, dict):
                obj = properties.get("cmis:objectId")
                if isinstance(obj, dict) and "value" in obj:
                    return str(obj["value"])
            return str(data.get("id", "unknown"))
        return "unknown"


# ---------------------------------------------------------------------------
# Helpers a nivel de módulo
# ---------------------------------------------------------------------------


def _truncate(text: str) -> str:
    if len(text) <= _RESPONSE_BODY_TRUNCATION:
        return text
    return text[:_RESPONSE_BODY_TRUNCATION] + "...(truncated)"


def _http_error(resp: httpx.Response) -> CMISClientError | CMISServerError:
    """120: construye la excepción HTTP decodificando el body SOLO acá.

    Pre-120 los GET decodificaban ``resp.text`` completo antes de
    chequear el status — trabajo tirado en cada respuesta 200.
    """
    body = _truncate(resp.text)
    if resp.status_code >= 500:
        return CMISServerError(status_code=resp.status_code, response_body=body)
    return CMISClientError(status_code=resp.status_code, response_body=body)
