"""Semáforo Pie Andino: seguimiento diario de tareas por hora y tipo de día."""
import csv
import io
import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

import holidays
import psycopg
from psycopg.rows import dict_row
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

TZ = ZoneInfo(os.environ.get("ZONA_HORARIA", "America/Santiago"))
TOLERANCIA_MIN = int(os.environ.get("TOLERANCIA_MIN", "0"))
DIAS_RELLENO = 31  # días hacia atrás que se completan si la app estuvo detenida
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/semaforo")
ADMIN_USUARIO = os.environ.get("ADMIN_USUARIO", "admin")
ADMIN_CLAVE = os.environ.get("ADMIN_CLAVE", "")

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
ALTER TABLE base_semaforo ADD COLUMN IF NOT EXISTS registrado_por TEXT;
ALTER TABLE base_semaforo ADD COLUMN IF NOT EXISTS modificado_por TEXT;
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    usuario TEXT NOT NULL UNIQUE,
    nombre TEXT NOT NULL,
    clave_hash TEXT NOT NULL,
    rol TEXT NOT NULL DEFAULT 'operador' CHECK (rol IN ('admin','operador')),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    creado TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS configuracion (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL DEFAULT ''
);
-- Historial de cambios hechos por un administrador (deshacer / editar)
CREATE TABLE IF NOT EXISTS historial_cambios (
    id SERIAL PRIMARY KEY,
    registro_id INT NOT NULL,
    usuario TEXT NOT NULL,
    accion TEXT NOT NULL,
    antes TEXT,
    despues TEXT,
    fecha TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER") is not None,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
logging.basicConfig(level=logging.INFO)

_feriados = holidays.country_holidays("CL")


def conectar():
    return psycopg.connect(
        DATABASE_URL, row_factory=dict_row, autocommit=False,
        connect_timeout=15, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
    )


def ahora():
    return datetime.now(TZ)


def feriado(d: date):
    return _feriados.get(d)


def tipo_dia(d: date) -> str:
    """Feriados de Chile usan las tareas de Domingo."""
    if d.weekday() == 6 or feriado(d):
        return "Domingo"
    return "Sábado" if d.weekday() == 5 else "Hábil"


def iniciar_bd():
    with conectar() as cx:
        cx.execute(SCHEMA)
        cx.cursor().executemany(
            "INSERT INTO parametro_tareas (tipo_dia, hora, tarea) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            PARAMETRO_TAREAS,
        )
        cx.cursor().executemany(
            "INSERT INTO responsables (nombre) VALUES (%s) ON CONFLICT DO NOTHING",
            [(n,) for n in RESPONSABLES],
        )
        cx.execute("INSERT INTO configuracion (clave, valor) VALUES ('contacto', '') ON CONFLICT DO NOTHING")
        hay_admin = cx.execute("SELECT 1 FROM usuarios WHERE rol='admin' AND activo").fetchone()
        if not hay_admin and ADMIN_CLAVE:
            cx.execute(
                """INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES (%s, 'Administrador', %s, 'admin')
                   ON CONFLICT (usuario) DO UPDATE SET clave_hash = EXCLUDED.clave_hash, rol='admin', activo=TRUE""",
                (ADMIN_USUARIO, generate_password_hash(ADMIN_CLAVE)),
            )
            app.logger.info("Administrador inicial '%s' creado", ADMIN_USUARIO)


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


def observacion(fecha: date, hora, ejecutada: datetime) -> str:
    programada = datetime.combine(fecha, hora, TZ) + timedelta(minutes=TOLERANCIA_MIN)
    return "A tiempo" if ejecutada.replace(second=0, microsecond=0) <= programada else "Atrasada"


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
        "registrado_por": r.get("registrado_por"),
        "modificado_por": r.get("modificado_por"),
    }


# ---------- Sesión ----------

def usuario_actual():
    return session.get("u")


def requiere_login(f):
    @wraps(f)
    def w(*a, **k):
        if not usuario_actual():
            return jsonify(error="Tu sesión terminó. Vuelve a iniciar sesión."), 401
        return f(*a, **k)
    return w


def requiere_admin(f):
    @wraps(f)
    def w(*a, **k):
        u = usuario_actual()
        if not u:
            return jsonify(error="Tu sesión terminó. Vuelve a iniciar sesión."), 401
        if u["rol"] != "admin":
            return jsonify(error="Solo un administrador puede hacer esto."), 403
        return f(*a, **k)
    return w


@app.before_request
def verificar_sesion_vigente():
    """Cierra la sesión si el usuario fue desactivado o cambió de rol."""
    u = usuario_actual()
    if not u or request.path.startswith("/static") or request.path == "/healthz":
        return
    if session.get("verificado", 0) > time.time() - 60:
        return
    with conectar() as cx:
        r = cx.execute("SELECT id, usuario, nombre, rol, activo FROM usuarios WHERE id=%s", (u["id"],)).fetchone()
    if not r or not r["activo"]:
        session.clear()
        return
    session["u"] = {"id": r["id"], "usuario": r["usuario"], "nombre": r["nombre"], "rol": r["rol"]}
    session["verificado"] = time.time()


@app.before_request
def solo_json_en_post():
    # Protección CSRF: los formularios de otros sitios no pueden enviar JSON con cookies.
    if request.method == "POST" and request.path.startswith("/api/") and not request.is_json:
        return jsonify(error="Solicitud no válida."), 400


@app.get("/")
def index():
    if not usuario_actual():
        return redirect("/login")
    return send_from_directory(app.static_folder, "index.html")


@app.get("/login")
def login_page():
    return send_from_directory(app.static_folder, "login.html")


@app.get("/admin")
def admin_page():
    u = usuario_actual()
    if not u:
        return redirect("/login")
    if u["rol"] != "admin":
        return redirect("/")
    return send_from_directory(app.static_folder, "admin.html")


@app.post("/api/login")
def api_login():
    d = request.get_json(silent=True) or {}
    usuario = (d.get("usuario") or "").strip().lower()
    clave = d.get("clave") or ""
    with conectar() as cx:
        r = cx.execute("SELECT * FROM usuarios WHERE lower(usuario)=%s AND activo", (usuario,)).fetchone()
    if not r or not check_password_hash(r["clave_hash"], clave):
        time.sleep(0.7)
        return jsonify(error="Usuario o contraseña incorrectos."), 401
    session.clear()
    session.permanent = True
    session["u"] = {"id": r["id"], "usuario": r["usuario"], "nombre": r["nombre"], "rol": r["rol"]}
    session["verificado"] = time.time()
    return jsonify(ok=True)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/mi-clave")
@requiere_login
def api_mi_clave():
    d = request.get_json(silent=True) or {}
    actual, nueva = d.get("actual") or "", d.get("nueva") or ""
    if len(nueva) < 8:
        return jsonify(error="La nueva contraseña debe tener al menos 8 caracteres."), 400
    with conectar() as cx:
        r = cx.execute("SELECT clave_hash FROM usuarios WHERE id=%s", (usuario_actual()["id"],)).fetchone()
        if not check_password_hash(r["clave_hash"], actual):
            return jsonify(error="La contraseña actual no es correcta."), 400
        cx.execute("UPDATE usuarios SET clave_hash=%s WHERE id=%s",
                   (generate_password_hash(nueva), usuario_actual()["id"]))
    return jsonify(ok=True)


# ---------- Tareas ----------

@app.get("/api/dia")
@requiere_login
def api_dia():
    n = ahora()
    hoy = n.date()
    try:
        fecha = date.fromisoformat(request.args["fecha"]) if request.args.get("fecha") else hoy
    except ValueError:
        return jsonify(error="Fecha no válida."), 400
    fecha = min(fecha, hoy)
    with conectar() as cx:
        sincronizar(cx)
        filas = cx.execute("SELECT * FROM base_semaforo WHERE fecha = %s ORDER BY hora, tarea", (fecha,)).fetchall()
        resp = cx.execute("SELECT nombre FROM responsables WHERE activo ORDER BY id").fetchall()
        contacto = cx.execute("SELECT valor FROM configuracion WHERE clave='contacto'").fetchone()["valor"]
    return jsonify(
        fecha=fecha.isoformat(),
        hoy=hoy.isoformat(),
        hora=n.strftime("%H:%M"),
        tipo_dia=tipo_dia(fecha),
        feriado=feriado(fecha),
        tolerancia_min=TOLERANCIA_MIN,
        responsables=[r["nombre"] for r in resp],
        tareas=[serializar(r) for r in filas],
        contacto=contacto,
        usuario=usuario_actual(),
    )


# Compatibilidad con la versión anterior de la página
app.add_url_rule("/api/hoy", "api_hoy", api_dia)


def _responsable_valido(cx, nombre):
    return cx.execute("SELECT 1 FROM responsables WHERE nombre = %s AND activo", (nombre,)).fetchone()


@app.post("/api/ejecutar/<int:reg_id>")
@requiere_login
def api_ejecutar(reg_id):
    datos = request.get_json(silent=True) or {}
    responsable = (datos.get("responsable") or "").strip()
    n = ahora()
    with conectar() as cx:
        if not _responsable_valido(cx, responsable):
            return jsonify(error="Elige un responsable de la lista."), 400
        r = cx.execute("SELECT * FROM base_semaforo WHERE id = %s FOR UPDATE", (reg_id,)).fetchone()
        if not r:
            return jsonify(error="La tarea no existe."), 404
        if r["fecha"] != n.date():
            return jsonify(error="Solo se pueden ejecutar tareas del día en curso."), 409
        if r["estatus"] == "Ejecutada":
            return jsonify(error="Esta tarea ya fue ejecutada."), 409
        r = cx.execute(
            """UPDATE base_semaforo
               SET estatus='Ejecutada', responsable=%s, ejecutada=%s, observacion=%s,
                   registrado_por=%s, actualizado=now()
               WHERE id=%s RETURNING *""",
            (responsable, n, observacion(r["fecha"], r["hora"], n), usuario_actual()["usuario"], reg_id),
        ).fetchone()
    return jsonify(serializar(r))


def _resumen(r):
    s = serializar(r)
    return f'{s["estatus"]} | {s["responsable"] or "-"} | {s["ejecutada"] or "-"} | {s["observacion"] or "-"}'


@app.post("/api/deshacer/<int:reg_id>")
@requiere_admin
def api_deshacer(reg_id):
    """Vuelve una tarea ejecutada a Pendiente (o No Realizada si ya pasó su hora)."""
    with conectar() as cx:
        r = cx.execute("SELECT * FROM base_semaforo WHERE id=%s FOR UPDATE", (reg_id,)).fetchone()
        if not r:
            return jsonify(error="La tarea no existe."), 404
        if r["estatus"] != "Ejecutada":
            return jsonify(error="La tarea no está marcada como ejecutada."), 409
        nuevo = cx.execute(
            """UPDATE base_semaforo SET estatus='Pendiente', responsable=NULL, ejecutada=NULL,
                   observacion='', modificado_por=%s, actualizado=now()
               WHERE id=%s RETURNING *""",
            (usuario_actual()["usuario"], reg_id),
        ).fetchone()
        sincronizar(cx)
        nuevo = cx.execute("SELECT * FROM base_semaforo WHERE id=%s", (reg_id,)).fetchone()
        cx.execute(
            "INSERT INTO historial_cambios (registro_id, usuario, accion, antes, despues) VALUES (%s,%s,'deshacer',%s,%s)",
            (reg_id, usuario_actual()["usuario"], _resumen(r), _resumen(nuevo)),
        )
    return jsonify(serializar(nuevo))


@app.post("/api/editar/<int:reg_id>")
@requiere_admin
def api_editar(reg_id):
    """Corrige el responsable y/o la hora de ejecución; la observación se recalcula."""
    d = request.get_json(silent=True) or {}
    responsable = (d.get("responsable") or "").strip()
    hora_ej = (d.get("hora_ejecutada") or "").strip()
    with conectar() as cx:
        if not _responsable_valido(cx, responsable):
            return jsonify(error="Elige un responsable de la lista."), 400
        r = cx.execute("SELECT * FROM base_semaforo WHERE id=%s FOR UPDATE", (reg_id,)).fetchone()
        if not r:
            return jsonify(error="La tarea no existe."), 404
        try:
            hh, mm = (int(x) for x in hora_ej.split(":"))
            ej = datetime.combine(r["fecha"], datetime.min.time(), TZ).replace(hour=hh, minute=mm)
        except (ValueError, TypeError):
            return jsonify(error="Indica la hora de ejecución en formato HH:MM."), 400
        if ej > ahora():
            return jsonify(error="La hora de ejecución no puede ser futura."), 400
        nuevo = cx.execute(
            """UPDATE base_semaforo SET estatus='Ejecutada', responsable=%s, ejecutada=%s, observacion=%s,
                   registrado_por=COALESCE(registrado_por, %s), modificado_por=%s, actualizado=now()
               WHERE id=%s RETURNING *""",
            (responsable, ej, observacion(r["fecha"], r["hora"], ej),
             usuario_actual()["usuario"], usuario_actual()["usuario"], reg_id),
        ).fetchone()
        cx.execute(
            "INSERT INTO historial_cambios (registro_id, usuario, accion, antes, despues) VALUES (%s,%s,'editar',%s,%s)",
            (reg_id, usuario_actual()["usuario"], _resumen(r), _resumen(nuevo)),
        )
    return jsonify(serializar(nuevo))


@app.get("/api/base.csv")
@requiere_login
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
    w.writerow(["Fecha", "Hora", "Tipo dia", "Tarea", "Responsable", "Estatus", "Ejecutada", "Observacion",
                "Registrado por", "Modificado por"])
    for r in filas:
        s = serializar(r)
        w.writerow([s["fecha"], s["hora"], s["tipo_dia"], s["tarea"], s["responsable"] or "",
                    s["estatus"], s["ejecutada"] or "", s["observacion"],
                    s["registrado_por"] or "", s["modificado_por"] or ""])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=base_semaforo.csv"})


# ---------- Administración ----------

@app.get("/api/admin/datos")
@requiere_admin
def api_admin_datos():
    with conectar() as cx:
        us = cx.execute("SELECT id, usuario, nombre, rol, activo FROM usuarios ORDER BY activo DESC, nombre").fetchall()
        rs = cx.execute("SELECT id, nombre, activo FROM responsables ORDER BY activo DESC, id").fetchall()
        contacto = cx.execute("SELECT valor FROM configuracion WHERE clave='contacto'").fetchone()["valor"]
        hist = cx.execute(
            """SELECT h.fecha, h.usuario, h.accion, h.antes, h.despues, b.fecha AS dia, b.hora, b.tarea
               FROM historial_cambios h LEFT JOIN base_semaforo b ON b.id = h.registro_id
               ORDER BY h.fecha DESC LIMIT 50"""
        ).fetchall()
    return jsonify(
        usuarios=us, responsables=rs, contacto=contacto, yo=usuario_actual(),
        historial=[{
            "cuando": h["fecha"].astimezone(TZ).strftime("%d-%m-%Y %H:%M"), "usuario": h["usuario"],
            "accion": h["accion"], "antes": h["antes"], "despues": h["despues"],
            "tarea": f'{h["dia"].strftime("%d-%m-%Y") if h["dia"] else ""} {h["hora"].strftime("%H:%M") if h["hora"] else ""} {h["tarea"] or ""}'.strip(),
        } for h in hist],
    )


@app.post("/api/admin/usuarios")
@requiere_admin
def api_admin_crear_usuario():
    d = request.get_json(silent=True) or {}
    usuario = (d.get("usuario") or "").strip().lower()
    nombre = (d.get("nombre") or "").strip()
    rol = d.get("rol") if d.get("rol") in ("admin", "operador") else "operador"
    clave = d.get("clave") or ""
    if not usuario or not nombre:
        return jsonify(error="Completa el usuario y el nombre."), 400
    if len(clave) < 8:
        return jsonify(error="La contraseña debe tener al menos 8 caracteres."), 400
    with conectar() as cx:
        if cx.execute("SELECT 1 FROM usuarios WHERE lower(usuario)=%s", (usuario,)).fetchone():
            return jsonify(error="Ese nombre de usuario ya existe."), 409
        cx.execute("INSERT INTO usuarios (usuario, nombre, clave_hash, rol) VALUES (%s,%s,%s,%s)",
                   (usuario, nombre, generate_password_hash(clave), rol))
    return jsonify(ok=True)


@app.post("/api/admin/usuarios/<int:uid>")
@requiere_admin
def api_admin_editar_usuario(uid):
    d = request.get_json(silent=True) or {}
    with conectar() as cx:
        u = cx.execute("SELECT * FROM usuarios WHERE id=%s FOR UPDATE", (uid,)).fetchone()
        if not u:
            return jsonify(error="El usuario no existe."), 404
        rol = d.get("rol", u["rol"])
        activo = bool(d.get("activo", u["activo"]))
        if rol not in ("admin", "operador"):
            return jsonify(error="Rol no válido."), 400
        if u["rol"] == "admin" and (rol != "admin" or not activo):
            otros = cx.execute("SELECT count(*) AS n FROM usuarios WHERE rol='admin' AND activo AND id<>%s", (uid,)).fetchone()
            if otros["n"] == 0:
                return jsonify(error="Debe quedar al menos un administrador activo."), 409
        cx.execute("UPDATE usuarios SET rol=%s, activo=%s WHERE id=%s", (rol, activo, uid))
        if d.get("clave"):
            if len(d["clave"]) < 8:
                return jsonify(error="La contraseña debe tener al menos 8 caracteres."), 400
            cx.execute("UPDATE usuarios SET clave_hash=%s WHERE id=%s", (generate_password_hash(d["clave"]), uid))
    return jsonify(ok=True)


@app.post("/api/admin/responsables")
@requiere_admin
def api_admin_responsables():
    d = request.get_json(silent=True) or {}
    with conectar() as cx:
        if d.get("id"):
            cx.execute("UPDATE responsables SET activo=%s WHERE id=%s", (bool(d.get("activo")), d["id"]))
        else:
            nombre = (d.get("nombre") or "").strip()
            if not nombre:
                return jsonify(error="Escribe el nombre del responsable."), 400
            cx.execute("INSERT INTO responsables (nombre) VALUES (%s) ON CONFLICT (nombre) DO UPDATE SET activo=TRUE",
                       (nombre,))
    return jsonify(ok=True)


@app.post("/api/admin/contacto")
@requiere_admin
def api_admin_contacto():
    d = request.get_json(silent=True) or {}
    with conectar() as cx:
        cx.execute("UPDATE configuracion SET valor=%s WHERE clave='contacto'", ((d.get("contacto") or "").strip()[:500],))
    return jsonify(ok=True)


@app.errorhandler(Exception)
def error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception("Error en %s", request.path)
    return jsonify(error="Error del servidor: " + type(e).__name__), 500


@app.get("/healthz")
def healthz():
    return "ok"


iniciar_bd()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
