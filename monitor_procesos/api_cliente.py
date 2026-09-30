"""Cliente para la API no oficial del portal de la Rama Judicial (CPNU).

Ver monitor-procesos-spec.md, sección 3, para el detalle de los endpoints
y las particularidades de la respuesta que este módulo ya tiene en cuenta.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://consultaprocesos.ramajudicial.gov.co:448"
USER_AGENT = "monitor-procesos-judiciales/0.1 (uso personal; contacto: tu-email@ejemplo.com)"


class ErrorConsulta(Exception):
    """La consulta a un radicado/proceso puntual falló tras los reintentos.

    Quien la captura debe registrar el error y continuar con los demás
    procesos de la corrida (sección 7 del spec) — nunca debe abortar todo.
    """


class RespuestaInesperada(Exception):
    """La API respondió con una forma distinta a la esperada.

    Probablemente cambió la API sin aviso. Debe abortar la corrida completa
    con un mensaje claro (sección 7 del spec), no intentar adivinar.
    """


class NoEncontrado(Exception):
    """La API respondió 404 con un cuerpo tipo {"StatusCode":404,"Message":...}.

    Esta API usa 404 como respuesta "blanda" para radicado sin resultados,
    idProceso inexistente o parámetro inválido — no es un fallo transitorio
    de red, así que _get no lo reintenta. Cada método público decide qué
    hacer con esto (ver consultar_radicado).
    """

    def __init__(self, mensaje_api: str | None):
        self.mensaje_api = mensaje_api
        super().__init__(mensaje_api or "404 sin cuerpo JSON reconocible")


@dataclass
class ConfigRed:
    pausa_entre_peticiones: float = 1.0
    timeout: float = 30.0
    reintentos: int = 3
    esperas: tuple[float, ...] = (2.0, 8.0, 30.0)


class ClienteAPI:
    def __init__(self, config: ConfigRed | None = None, base_url: str = BASE_URL):
        self.config = config or ConfigRed()
        self.base_url = base_url
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._ultima_peticion: float | None = None

    def _esperar_pausa(self) -> None:
        if self._ultima_peticion is None:
            return
        transcurrido = time.monotonic() - self._ultima_peticion
        faltante = self.config.pausa_entre_peticiones - transcurrido
        if faltante > 0:
            time.sleep(faltante)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET con pausa, timeout y reintentos.

        Solo se reintentan fallos que pueden ser transitorios: errores de
        red/timeout y 5xx del servidor. Un 4xx es una respuesta determinista
        — la misma petición va a dar el mismo resultado siempre — así que no
        tiene sentido reintentarla. El 404 en particular es el código que
        esta API usa para "sin resultados" y se señaliza aparte (NoEncontrado)
        en vez de tratarse como fallo.
        """
        url = f"{self.base_url}{path}"
        intentos_totales = self.config.reintentos + 1
        ultimo_error: Exception | None = None

        for intento in range(1, intentos_totales + 1):
            self._esperar_pausa()
            self._ultima_peticion = time.monotonic()
            try:
                resp = self._session.get(url, params=params, timeout=self.config.timeout)
            except requests.RequestException as exc:
                ultimo_error = exc
                logger.warning(
                    "Intento %d/%d (red) falló para %s: %s", intento, intentos_totales, url, exc
                )
                if intento < intentos_totales:
                    time.sleep(self.config.esperas[intento - 1])
                continue

            if resp.status_code == 404:
                mensaje = None
                try:
                    mensaje = resp.json().get("Message")
                except ValueError:
                    pass
                raise NoEncontrado(mensaje)

            # 403 se observó en producción viniendo de un Azure Application
            # Gateway delante de la API (no de la API misma: cuerpo HTML, no
            # el JSON {"StatusCode":...} de sus 404). Apareció al final de una
            # corrida de ~40 peticiones seguidas — más parece un bloqueo
            # transitorio de la capa WAF que una respuesta determinista, así
            # que sí se reintenta (a diferencia del 404, que es la propia API
            # diciendo "sin resultados" de forma consistente).
            if resp.status_code == 403 or 500 <= resp.status_code < 600:
                ultimo_error = requests.HTTPError(f"{resp.status_code} en {url}")
                logger.warning(
                    "Intento %d/%d (%d) falló para %s",
                    intento,
                    intentos_totales,
                    resp.status_code,
                    url,
                )
                if intento < intentos_totales:
                    time.sleep(self.config.esperas[intento - 1])
                continue

            if resp.status_code >= 400:
                # 4xx que no es el 404 "blando" ni el 403 del gateway: no es
                # transitorio, no se reintenta.
                raise ErrorConsulta(
                    f"{resp.status_code} respondido por {url}: {resp.text[:300]!r}"
                )

            try:
                return resp.json()
            except ValueError as exc:
                raise RespuestaInesperada(f"JSON inválido en {url}: {exc}") from exc

        raise ErrorConsulta(
            f"No se pudo consultar {url} tras {intentos_totales} intentos"
        ) from ultimo_error

    @staticmethod
    def _strip_dict(d: dict[str, Any]) -> dict[str, Any]:
        return {k: (v.strip() if isinstance(v, str) else v) for k, v in d.items()}

    def consultar_radicado(self, numero_radicacion: str) -> list[dict[str, Any]]:
        """Lista completa de 'procesos' para un radicado de 23 dígitos.

        Lista vacía significa "sin resultados" (incluye el caso en que la API
        responde 404, que es como señaliza esto este endpoint). Usa
        SoloActivos=false siempre — no filtrar procesos inactivos acá, eso se
        decide después (sección 3.1 del spec).
        """
        try:
            data = self._get(
                "/api/v2/Procesos/Consulta/NumeroRadicacion",
                {"numero": numero_radicacion, "SoloActivos": "false", "pagina": 1},
            )
        except NoEncontrado:
            return []

        if not isinstance(data.get("procesos"), list):
            raise RespuestaInesperada(
                f"La respuesta para radicado {numero_radicacion} no trae "
                f"'procesos' como lista: {data!r}"
            )
        return [self._strip_dict(p) for p in data["procesos"]]

    def consultar_actuaciones(
        self, id_proceso: int, pagina: int = 1
    ) -> tuple[int, list[dict[str, Any]]]:
        """(cantidad_total, actuaciones_de_la_pagina) para un idProceso.

        cantidad_total sale de paginacion.cantidadRegistros — nunca de
        len(actuaciones), porque la página 1 puede no traerlas todas
        (sección 3.2 del spec).

        Un idProceso normalmente viene de un consultar_radicado exitoso, así
        que un 404 acá es anómalo (el proceso desapareció entre una consulta
        y otra) y se deja propagar como NoEncontrado en vez de silenciarlo.
        """
        data = self._get(f"/api/v2/Proceso/Actuaciones/{id_proceso}", {"pagina": pagina})

        if not isinstance(data.get("actuaciones"), list) or "paginacion" not in data:
            raise RespuestaInesperada(
                f"La respuesta de actuaciones para idProceso={id_proceso} no "
                f"tiene la forma esperada: {data!r}"
            )
        try:
            cantidad_total = int(data["paginacion"]["cantidadRegistros"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RespuestaInesperada(
                f"paginacion.cantidadRegistros ausente o inválido para "
                f"idProceso={id_proceso}: {data.get('paginacion')!r}"
            ) from exc

        actuaciones = [self._strip_dict(a) for a in data["actuaciones"]]
        return cantidad_total, actuaciones


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Sin valores por defecto a propósito: no se debe hardcodear en el
    # repositorio un radicado real de un caso real. Pasar un radicado válido
    # (23 dígitos, cualquiera, público) y su idProceso por variables de
    # entorno para probar contra la API en vivo:
    #   RADICADO_PRUEBA=... ID_PROCESO_PRUEBA=... python -m monitor_procesos.api_cliente
    RADICADO_CONOCIDO = os.environ.get("RADICADO_PRUEBA")
    ID_PROCESO_CONOCIDO = os.environ.get("ID_PROCESO_PRUEBA")

    if not RADICADO_CONOCIDO or not ID_PROCESO_CONOCIDO:
        print(
            "Faltan RADICADO_PRUEBA / ID_PROCESO_PRUEBA en el entorno — se "
            "omite la prueba contra la API en vivo (ver comentario arriba)."
        )
        raise SystemExit(0)

    ID_PROCESO_CONOCIDO = int(ID_PROCESO_CONOCIDO)

    cliente = ClienteAPI()

    print(f"--- consultar_radicado({RADICADO_CONOCIDO!r}) ---")
    procesos = cliente.consultar_radicado(RADICADO_CONOCIDO)
    print(f"{len(procesos)} proceso(s) encontrado(s)")
    for p in procesos:
        print(
            f"  idProceso={p['idProceso']} despacho={p['despacho']!r} "
            f"fechaUltimaActuacion={p['fechaUltimaActuacion']} esPrivado={p['esPrivado']}"
        )

    print(f"\n--- consultar_actuaciones({ID_PROCESO_CONOCIDO}) ---")
    total, actuaciones = cliente.consultar_actuaciones(ID_PROCESO_CONOCIDO)
    print(f"cantidad_total={total}, página trae {len(actuaciones)} actuaciones")
    if actuaciones:
        a = actuaciones[0]
        print(
            f"  más reciente: {a['fechaActuacion']} — {a['actuacion']!r} — "
            f"anotación={a['anotacion']!r}"
        )

    print("\n--- radicado inexistente (debe dar 0 procesos, sin reintentos) ---")
    t0 = time.monotonic()
    vacio = cliente.consultar_radicado("0" * 23)
    duracion = time.monotonic() - t0
    print(f"  {len(vacio)} proceso(s) (esperado: 0) — resuelto en {duracion:.2f}s")

    print("\n--- idProceso inexistente (debe propagar NoEncontrado) ---")
    try:
        cliente.consultar_actuaciones(1)
    except NoEncontrado as exc:
        print(f"  NoEncontrado como se esperaba: {exc}")
