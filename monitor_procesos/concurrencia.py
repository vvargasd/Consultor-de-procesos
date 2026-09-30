"""Lockfile — sección 8 del spec: una sola corrida a la vez.

Si una corrida se atrasa y el timer dispara la siguiente, la segunda debe
salir sin hacer nada (no es un error, es el comportamiento esperado).
"""

from __future__ import annotations

import fcntl
from typing import IO


class BloqueadoError(Exception):
    """Ya hay otra corrida en curso con el mismo lockfile."""


def adquirir_lock(ruta_lockfile: str) -> IO:
    """Devuelve el file handle abierto (mantenerlo referenciado mientras dure
    la corrida — el lock se libera solo al cerrarlo o al terminar el
    proceso, incluso si el proceso muere sin limpiar).
    """
    f = open(ruta_lockfile, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise BloqueadoError(f"ya hay una corrida en curso ({ruta_lockfile})")
    return f


def _intentar_en_otro_proceso(ruta_lock, cola) -> None:
    """A nivel de módulo (no anidada) para que multiprocessing pueda
    localizarla al lanzar el proceso hijo (spawn/forkserver la buscan por
    nombre importable, no funciona con funciones definidas dentro de un
    bloque `if __name__ == "__main__":`)."""
    try:
        adquirir_lock(ruta_lock)
        cola.put("adquirido")
    except BloqueadoError:
        cola.put("bloqueado")


def _autoprueba() -> None:
    """Correr con `python -c "from monitor_procesos.concurrencia import
    _autoprueba; _autoprueba()"`, no con `python -m` — incluso con la
    función de proceso a nivel de módulo, `-m` ejecuta este archivo como
    __main__, una identidad de módulo distinta de la que ven los procesos
    hijos al reimportarlo."""
    import multiprocessing
    import tempfile
    import time
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        ruta = str(Path(tmp) / "prueba.lock")

        # 1. adquirir en el mismo proceso: un segundo intento SÍ debería
        #    poder adquirirlo porque flock() por PID no bloquea contra sí
        #    mismo con un fd distinto del mismo proceso -- por eso la prueba
        #    real usa un proceso hijo separado.
        primero = adquirir_lock(ruta)
        print("1. primer lock adquirido: OK")

        cola = multiprocessing.Queue()
        proceso = multiprocessing.Process(
            target=_intentar_en_otro_proceso, args=(ruta, cola)
        )
        proceso.start()
        resultado = cola.get(timeout=5)
        proceso.join(timeout=5)

        assert resultado == "bloqueado", f"se esperaba 'bloqueado', se obtuvo {resultado!r}"
        print("2. segundo proceso bloqueado mientras el primero tiene el lock: OK")

        primero.close()
        time.sleep(0.2)

        proceso2 = multiprocessing.Process(
            target=_intentar_en_otro_proceso, args=(ruta, cola)
        )
        proceso2.start()
        resultado2 = cola.get(timeout=5)
        proceso2.join(timeout=5)
        assert resultado2 == "adquirido", f"se esperaba 'adquirido', se obtuvo {resultado2!r}"
        print("3. tras liberar, un nuevo proceso sí puede adquirir el lock: OK")

    print("\nTodas las pruebas de concurrencia pasaron.")


if __name__ == "__main__":
    _autoprueba()
