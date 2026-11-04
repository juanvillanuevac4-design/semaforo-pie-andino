# Semáforo Pie Andino

Semáforo diario de tareas por hora y tipo de día (Hábil, Sábado, Domingo), basado en `semaforo.xlsx`.

- **Verde / Ejecutada:** se presionó "Ejecutada". Observación "A tiempo" si fue a la hora programada o antes, "Atrasada" si fue después.
- **Amarillo / Pendiente:** aún no llega la hora programada.
- **Rojo / No Realizada:** pasó la hora sin ejecutarse. Queda guardada con observación "Sin ejecutar".

Cada tarea del día tiene una fila en la tabla `base_semaforo` (columnas de la hoja "Base de Datos": Fecha, Hora, Tipo dia, Tarea, Responsable, Estatus, Ejecutada, Observacion). Las filas se crean al empezar el día y se actualizan solas; si el servicio estuvo dormido, al despertar completa los días que faltan (hasta 31 días atrás).

## Configuración

| Variable | Uso | Valor por defecto |
|---|---|---|
| `DATABASE_URL` | Conexión Postgres (Neon, Render, etc.) | — |
| `ZONA_HORARIA` | Zona horaria de las horas programadas | `America/Santiago` |
| `TOLERANCIA_MIN` | Minutos de gracia antes de marcar Atrasada / No Realizada | `0` |

Las tareas y responsables se cargan desde las tablas `parametro_tareas` y `responsables` (sembradas con los datos del Excel al primer arranque).

## Rutas

- `/` interfaz (línea de tiempo)
- `GET /api/hoy` tareas del día
- `POST /api/ejecutar/<id>` con `{"responsable": "..."}`
- `GET /api/base.csv?desde=AAAA-MM-DD&hasta=AAAA-MM-DD` descarga de la Base semáforo

## Ejecutar localmente

```bash
pip install -r requirements.txt
DATABASE_URL=postgresql://... gunicorn -w 1 --threads 4 app:app
```
