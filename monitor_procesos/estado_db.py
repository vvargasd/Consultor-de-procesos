"""Estado persistente en SQLite — sección 5 del spec.

Archivo separado del Excel (el Excel nunca se escribe). Guarda, por cada
idProceso visto alguna vez, los datos necesarios para la comparación de dos
niveles, más un historial de corridas para poder responder después
"¿por qué no me llegó tal aviso?".
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime

ESQUEMA = """
CREATE TABLE IF NOT EXISTS procesos (
    id_proceso INTEGER PRIMARY KEY,
    llave_proceso TEXT NOT NULL,
    id_conexion INTEGER,
    despacho TEXT,
    sujetos_procesales TEXT,
    es_privado INTEGER NOT NULL DEFAULT 0,
    fecha_ultima_actuacion TEXT,
    cantidad_actuaciones INTEGER,
    ultima_revision_ok TEXT
);

CREATE INDEX IF NOT EXISTS idx_procesos_llave ON procesos (llave_proceso);

CREATE TABLE IF NOT EXISTS historial_corridas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    procesos_consultados INTEGER NOT NULL,
    novedades INTEGER NOT NULL,
    errores INTEGER NOT NULL,
    detalle TEXT
);
"""


@dataclass(frozen=True)
class EstadoProceso:
    id_proceso: int
    llave_proceso: str
    id_conexion: int | None
    despacho: str | None
    sujetos_procesales: str | None
    es_privado: bool
    fecha_ultima_actuacion: date | None
    cantidad_actuaciones: int | None
    ultima_revision_ok: datetime | None


def conectar(ruta: str) -> sqlite3.Connection:
    conn = sqlite3.connect(ruta)
    conn.row_factory = sqlite3.Row
    conn.executescript(ESQUEMA)
    conn.commit()
    return conn


def obtener_proceso(conn: sqlite3.Connection, id_proceso: int) -> EstadoProceso | None:
    fila = conn.execute(
        "SELECT * FROM procesos WHERE id_proceso = ?", (id_proceso,)
    ).fetchone()
    if fila is None:
        return None
    return EstadoProceso(
        id_proceso=fila["id_proceso"],
        llave_proceso=fila["llave_proceso"],
        id_conexion=fila["id_conexion"],
        despacho=fila["despacho"],
        sujetos_procesales=fila["sujetos_procesales"],
        es_privado=bool(fila["es_privado"]),
        fecha_ultima_actuacion=(
            date.fromisoformat(fila["fecha_ultima_actuacion"])
            if fila["fecha_ultima_actuacion"]
            else None
        ),
        cantidad_actuaciones=fila["cantidad_actuaciones"],
        ultima_revision_ok=(
            datetime.fromisoformat(fila["ultima_revision_ok"])
            if fila["ultima_revision_ok"]
            else None
        ),
    )


def guardar_proceso(conn: sqlite3.Connection, estado: EstadoProceso) -> None:
    """Upsert. No debe llamarse para un proceso cuya consulta falló (sección 7
    del spec) — si se guardara, la novedad quedaría enmascarada en la
    corrida siguiente. La decisión de si la consulta fue exitosa es del
    llamador; esta función simplemente escribe lo que se le pasa.
    """
    conn.execute(
        """
        INSERT INTO procesos (
            id_proceso, llave_proceso, id_conexion, despacho,
            sujetos_procesales, es_privado, fecha_ultima_actuacion,
            cantidad_actuaciones, ultima_revision_ok
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id_proceso) DO UPDATE SET
            llave_proceso = excluded.llave_proceso,
            id_conexion = excluded.id_conexion,
            despacho = excluded.despacho,
            sujetos_procesales = excluded.sujetos_procesales,
            es_privado = excluded.es_privado,
            fecha_ultima_actuacion = excluded.fecha_ultima_actuacion,
            cantidad_actuaciones = excluded.cantidad_actuaciones,
            ultima_revision_ok = excluded.ultima_revision_ok
        """,
        (
            estado.id_proceso,
            estado.llave_proceso,
            estado.id_conexion,
            estado.despacho,
            estado.sujetos_procesales,
            int(estado.es_privado),
            estado.fecha_ultima_actuacion.isoformat() if estado.fecha_ultima_actuacion else None,
            estado.cantidad_actuaciones,
            estado.ultima_revision_ok.isoformat() if estado.ultima_revision_ok else None,
        ),
    )
    conn.commit()


def registrar_corrida(
    conn: sqlite3.Connection,
    timestamp: datetime,
    procesos_consultados: int,
    novedades: int,
    errores: int,
    detalle: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO historial_corridas
            (timestamp, procesos_consultados, novedades, errores, detalle)
        VALUES (?, ?, ?, ?, ?)
        """,
        (timestamp.isoformat(), procesos_consultados, novedades, errores, detalle),
    )
    conn.commit()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        ruta_db = str(Path(tmp) / "estado_prueba.sqlite3")
        with closing(conectar(ruta_db)) as conn:
            assert obtener_proceso(conn, 10000001) is None, "no debería existir aún"

            estado = EstadoProceso(
                id_proceso=10000001,
                llave_proceso="11001310301220200099900",
                id_conexion=999,
                despacho="JUZGADO 012 CIVIL DEL CIRCUITO DE BOGOTÁ",
                sujetos_procesales="Demandante: MARIA EJEMPLO DE PRUEBA",
                es_privado=False,
                fecha_ultima_actuacion=date(2026, 9, 15),
                cantidad_actuaciones=120,
                ultima_revision_ok=datetime.now(),
            )
            guardar_proceso(conn, estado)

            recuperado = obtener_proceso(conn, 10000001)
            assert recuperado is not None
            assert recuperado.fecha_ultima_actuacion == date(2026, 9, 15)
            assert recuperado.cantidad_actuaciones == 120
            print("guardar/obtener_proceso: OK")

            estado2 = EstadoProceso(**{**estado.__dict__, "cantidad_actuaciones": 121})
            guardar_proceso(conn, estado2)
            recuperado2 = obtener_proceso(conn, 10000001)
            assert recuperado2.cantidad_actuaciones == 121
            print("upsert (actualización): OK")

            registrar_corrida(conn, datetime.now(), procesos_consultados=32, novedades=1, errores=0)
            fila = conn.execute("SELECT * FROM historial_corridas").fetchone()
            assert fila["procesos_consultados"] == 32
            print("registrar_corrida: OK")

    print("\nTodas las pruebas de estado_db pasaron.")
