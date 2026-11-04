"""Semáforo Pie Andino: seguimiento diario de tareas por hora y tipo de día."""
import csv
import io
import os
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, jsonify, request, send_from_directory

TZ = ZoneInfo(os.environ.get("ZONA_HORARIA", "America/Santiago"))
TOLERANCIA_MIN = int(os.environ.get("TOLERANCIA_MIN", "0"))
DIAS_RELLENO = 31  # días hacia atrás que se completan si la app estuvo detenida
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/semaforo")

# Hoja "Parametro Tareas" del Excel semaforo.xlsx
PARAMETRO_TAREAS = [
    ("Hábil", "05:00", "Reporte de Dotacion de Flota"),
    ("Hábil", "08:00", "Reporte de Dotacion de Flota"),
    ("Hábil", "12:00", "Reporte de Dotacion de Flota"),
    ("Hábil", "16:00", "Reporte de Dotacion de Flota"),
    ("Hábil", "20:00", "Reporte de Dotacion de Flota"),
    ("Hábil", "05:00", "Reporte de Torniquete"),
    ("Sábado", "05:00", "Reporte de Torniquete"),
    ("Domingo", "05:00", "Reporte de Torniquete"),
    ("Hábil", "10:00", "Reporte de Asistencia"),
    ("Sábado", "10:00", "Reporte de Asistencia"),
    ("Domingo", "10:00", "Reporte de Asistencia"),
    ("Hábil", "02:00", "Reporte de Asistencia"),
    ("Sábado", "02:00", "Reporte de Asistencia"),
    ("Domingo", "02:00", "Reporte de Asistencia"),
    ("Hábil", "18:00", "Reporte de Asistencia"),
    ("Sábado", "18:00", "Reporte de Asistencia"),
    ("Domingo", "18:00", "Reporte de Asistencia"),
]
# Hoja "Lista Responsable"
RESPONSABLES = [
    "CRISTIAN RODRIGO LUCO REYES",
    "ERICK ALEJANDRO FALLAU MEZA",
    "ENRIQUE ESTEBAN ACEITUNO ROJAS",
    "Otro",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS parametro_tareas (
    id SERIAL PRIMARY KEY,
    tipo_dia TEXT NOT NULL CHECK (tipo_dia IN ('Hábil','Sábado','Domingo')),
    hora TIME NOT NULL,
    tarea TEXT NOT NULL,
    activa BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (tipo_dia, hora, tarea)
);
CREATE TABLE IF NOT EXISTS responsables (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);
-- "Base semáforo": columnas de la hoja "Base de Datos"
CREATE TABLE IF NOT EXISTS base_semaforo (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL,
    hora TIME NOT NULL,
    tipo_dia TEXT NOT NULL,
    tarea TEXT NOT NULL,
    responsable TEXT,
    estatus TEXT NOT NULL DEFAULT 'Pendiente'
        CHECK (estatus IN ('Pendiente','No Realizada','Ejecutada')),
    ejecutada TIMESTAMPTZ,
    observacion TEXT NOT NULL DEFAULT '',
    creado TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (fecha, hora, tarea)
);
CREATE INDEX IF NOT EXISTS base_semaforo_fecha ON base_semaforo (fecha);
"""

app = Flask(__name__, static_folder="static")
logging.basicConfig(level=logging.INFO)


def conectar():
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, autocommit=False,
        connect_timeout=15, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )


def ahora():
    return datetime.now(TZ)


def tipo_dia(d: date) -> str:
    return {5: "Sábado", 6: "Domingo"}.get(d.weekday(), "Hábil")


def iniciar_bd():
    with conectar() as cx:
        cx.execute(SCHEMA)
        cx.cursor().executemany(
            "INSERT INTO parametro_tareas (tipo_dia, hora, tarea) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            PARAMETRO_TAREAS,
        )
        cx.cursor().executemany(
            "INSERT INTO responsables (nombre) VALUES (%s) ON CONFLICT DO NOTHING",
            [(n,) for n in RESPONSABLES],
        )


def sincronizar(cx):
    """Crea los registros del día (y de días pasados sin registro) y marca lo no ejecutado.

    Cada tarea programada tiene una fila por día en base_semaforo desde que empieza el día,
    con estatus Pendiente. Cuando pasa su hora (más la tolerancia) sin ejecutarse,
    queda como No Realizada con observación "Sin ejecutar".
    """
    n = ahora()
    hoy = n.date()
    fila = cx.execute("SELECT max(fecha) AS f FROM base_semaforo").fetchone()
    desde = fila["f"] or hoy
    desde = max(desde, hoy - timedelta(days=DIAS_RELLENO))
    d = desde
    while d <= hoy:
        cx.execute(
            """INSERT INTO base_semaforo (fecha, hora, tipo_dia, tarea)
               SELECT %s, hora, tipo_dia, tarea FROM parametro_tareas
               WHERE activa AND tipo_dia = %s
               ON CONFLICT (fecha, hora, tarea) DO NOTHING""",
            (d, tipo_dia(d)),
        )
        d += timedelta(days=1)
    limite = (n - timedelta(minutes=TOLERANCIA_MIN)).replace(tzinfo=None)
    cx.execute(
        """UPDATE base_semaforo
           SET estatus = 'No Realizada', observacion = 'Sin ejecutar', actualizado = now()
           WHERE estatus = 'Pendiente' AND (fecha + hora) < date_trunc('minute', %s::timestamp)""",
        (limite,),
    )


def serializar(r):
    ej = r["ejecutada"].astimezone(TZ).strftime("%H:%M") if r["ejecutada"] else None
    return {
        "id": r["id"],
        "fecha": r["fecha"].isoformat(),
        "hora": r["hora"].strftime("%H:%M"),
        "tipo_dia": r["tipo_dia"],
        "tarea": r["tarea"],
        "responsable": r["responsable"],
        "estatus": r["estatus"],
        "ejecutada": ej,
        "observacion": r["observacion"],
    }


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/hoy")
def api_hoy():
    n = ahora()
    with conectar() as cx:
        sincronizar(cx)
        filas = cx.execute(
            "SELECT * FROM base_semaforo WHERE fecha = %s ORDER BY hora, tarea", (n.date(),)
        ).fetchall()
        resp = cx.execute(
            "SELECT nombre FROM responsables WHERE activo ORDER BY id"
        ).fetchall()
    return jsonify(
        fecha=n.date().isoformat(),
        hora=n.strftime("%H:%M"),
        tipo_dia=tipo_dia(n.date()),
        tolerancia_min=TOLERANCIA_MIN,
        responsables=[r["nombre"] for r in resp],
        tareas=[serializar(r) for r in filas],
    )


@app.post("/api/ejecutar/<int:reg_id>")
def api_ejecutar(reg_id):
    datos = request.get_json(silent=True) or {}
    responsable = (datos.get("responsable") or "").strip()
    n = ahora()
    with conectar() as cx:
        if not cx.execute(
            "SELECT 1 FROM responsables WHERE nombre = %s AND activo", (responsable,)
        ).fetchone():
            return jsonify(error="Elige un responsable de la lista."), 400
        r = cx.execute("SELECT * FROM base_semaforo WHERE id = %s FOR UPDATE", (reg_id,)).fetchone()
        if not r:
            return jsonify(error="La tarea no existe."), 404
        if r["fecha"] != n.date():
            return jsonify(error="Solo se pueden ejecutar tareas del día en curso."), 409
        if r["estatus"] == "Ejecutada":
            return jsonify(error="Esta tarea ya fue ejecutada."), 409
        programada = datetime.combine(r["fecha"], r["hora"], TZ) + timedelta(minutes=TOLERANCIA_MIN)
        obs = "A tiempo" if n.replace(second=0, microsecond=0) <= programada else "Atrasada"
        r = cx.execute(
            """UPDATE base_semaforo
               SET estatus='Ejecutada', responsable=%s, ejecutada=%s, observacion=%s, actualizado=now()
               WHERE id=%s RETURNING *""",
            (responsable, n, obs, reg_id),
        ).fetchone()
    return jsonify(serializar(r))


@app.get("/api/base.csv")
def api_csv():
    """Descarga la Base semáforo completa (o un rango ?desde=AAAA-MM-DD&hasta=AAAA-MM-DD)."""
    desde = request.args.get("desde") or "1900-01-01"
    hasta = request.args.get("hasta") or "2999-12-31"
    with conectar() as cx:
        sincronizar(cx)
        filas = cx.execute(
            "SELECT * FROM base_semaforo WHERE fecha BETWEEN %s AND %s ORDER BY fecha, hora, tarea",
            (desde, hasta),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Fecha", "Hora", "Tipo dia", "Tarea", "Responsable", "Estatus", "Ejecutada", "Observacion"])
    for r in filas:
        s = serializar(r)
        w.writerow([s["fecha"], s["hora"], s["tipo_dia"], s["tarea"], s["responsable"] or "",
                    s["estatus"], s["ejecutada"] or "", s["observacion"]])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=base_semaforo.csv"})


@app.errorhandler(Exception)
def error(e):
    app.logger.exception("Error en %s", request.path)
    return jsonify(error="Error del servidor: " + type(e).__name__), 500


@app.get("/healthz")
def healthz():
    return "ok"


iniciar_bd()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
