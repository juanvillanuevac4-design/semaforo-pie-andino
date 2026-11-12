# Semáforo Pie Andino

Semáforo diario de tareas por hora y tipo de día (Hábil, Sábado, Domingo), basado en `semaforo.xlsx`.

- **Verde / Ejecutada:** se presionó "Ejecutada". Observación "A tiempo" si fue a la hora programada o antes, "Atrasada" si fue después.
- **Amarillo / Pendiente:** aún no llega la hora programada.
- **Rojo / No Realizada:** pasó la hora sin ejecutarse. Queda guardada con observación "Sin ejecutar".

Tipo de día: lunes a viernes Hábil, sábado Sábado, domingo Domingo. Los feriados de Chile (librería `holidays`) usan las tareas de Domingo. No hay minutos de tolerancia.

## Usuarios

- Se ingresa con usuario y contraseña. Roles: **Operador** (marca, corrige y deshace tareas) y **Administrador** (además asigna el responsable del incumplimiento de las no ejecutadas y administra usuarios, la lista de responsables y el contacto en `/admin`).
- Cada corrección queda en la tabla `historial_cambios`; `base_semaforo` guarda quién marcó (`registrado_por`) y quién corrigió (`modificado_por`).
- El primer administrador se crea al arrancar con `ADMIN_USUARIO` / `ADMIN_CLAVE` si no existe ninguno.

Cada tarea del día tiene una fila en la tabla `base_semaforo` (columnas de la hoja "Base de Datos": Fecha, Hora, Tipo dia, Tarea, Responsable, Estatus, Ejecutada, Observacion). Las filas se crean al empezar el día y se actualizan solas; si el servicio estuvo dormido, al despertar completa los días que faltan (hasta 31 días atrás).

## Configuración

| Variable | Uso | Valor por defecto |
|---|---|---|
| `DATABASE_URL` | Conexión Postgres (Neon, Render, etc.) | — |
| `ZONA_HORARIA` | Zona horaria de las horas programadas | `America/Santiago` |
| `TOLERANCIA_MIN` | Minutos de gracia antes de marcar Atrasada / No Realizada | `0` |
| `SECRET_KEY` | Firma de la sesión (obligatoria en producción) | aleatoria |
| `ADMIN_USUARIO` / `ADMIN_CLAVE` | Administrador inicial | `admin` / — |

Las tareas y responsables se cargan desde las tablas `parametro_tareas` y `responsables` (sembradas con los datos del Excel al primer arranque).

## Rutas

- `/` interfaz (línea de tiempo)
- `/dashboard` tres propuestas de dashboard (A: resumen ejecutivo, B: mapa de calor, C: por responsable)
- `/login`, `/admin` (incluye la sección "Tareas no ejecutadas")
- `GET /api/dashboard?desde=&hasta=` registros del rango (máx. 366 días)
- `GET /api/admin/no-ejecutadas?desde=&hasta=`, `POST /api/admin/no-ejecutada/<id>` con `{"responsable", "comentario"}` (administrador): asigna el responsable del incumplimiento, elegido de la lista de usuarios activos; la tarea sigue como No Realizada
- `GET /api/dia?fecha=AAAA-MM-DD` tareas del día
- `POST /api/deshacer/<id>`, `POST /api/editar/<id>` (cualquier usuario; queda en `historial_cambios`)
- `POST /api/ejecutar/<id>` con `{"responsable": "..."}`
- `GET /api/base.csv?desde=AAAA-MM-DD&hasta=AAAA-MM-DD` descarga de la Base semáforo

## Ejecutar localmente

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://... gunicorn -w 1 --threads 4 app:app
```
