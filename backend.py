"""
FastAPI Backend — Invoice Agent
Servidor único. app.py es el módulo de procesamiento de correos.
"""
import logging
logging.getLogger("watchfiles").setLevel(logging.ERROR)

from dotenv import load_dotenv
load_dotenv()

import app as app_module

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import asyncio, threading, json, os, schedule
from datetime import datetime
from typing import List, Dict, Optional

fastapi_app = FastAPI(title="Invoice Agent API", version="5.0.0")
fastapi_app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ── LOG STORE ─────────────────────────────────────────────────────────────────
class LogStore:
    def __init__(self):
        self.logs: List[Dict] = []
        self.lock = threading.Lock()

    def add_log(self, message: str, level: str = "info", timestamp: str = None):
        if timestamp is None:
            timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append({"message": message, "level": level, "timestamp": timestamp})
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]

    def get_logs(self) -> List[Dict]:
        with self.lock:
            return self.logs.copy()

    def clear(self):
        with self.lock:
            self.logs = []


log_store = LogStore()
connected_clients: set = set()

# ── ESTADO SCHEDULER ──────────────────────────────────────────────────────────
scheduler_state = {
    "enabled": False,
    "mode": "interval",
    "interval_minutes": 30,
    "daily_time": "08:00",
    "next_run": None,
    "last_run": None,
    "running": False,
}
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()


# ── PROCESO PRINCIPAL ─────────────────────────────────────────────────────────
def run_process_with_logs():
    if scheduler_state["running"]:
        log_store.add_log("Proceso ya en ejecución, espera que termine", "warning")
        return

    scheduler_state["running"] = True
    scheduler_state["last_run"] = datetime.now().strftime("%H:%M %d/%m/%Y")

    import sys, io

    class LogCapture(io.TextIOBase):
        def write(self, msg):
            msg = msg.strip()
            if msg:
                level = "error"   if any(x in msg for x in ["Error", "error", "ERROR", "❌"]) else \
                        "success" if any(x in msg for x in ["completado", "guardada", "✅", "exitosamente", "Factura guardada"]) else \
                        "warning" if any(x in msg for x in ["Warning", "warning", "⚠️"]) else "info"
                log_store.add_log(msg, level)
            return len(msg if msg else "")
        def flush(self):
            pass

    old_stdout = sys.stdout
    sys.stdout = LogCapture()

    processed_path = getattr(app_module, 'PROCESSED_FILE', 'processed_emails.json')
    backup_ids = set()
    try:
        if os.path.exists(processed_path):
            with open(processed_path) as f:
                backup_ids = set(json.load(f))
        with open(processed_path, 'w') as f:
            json.dump([], f)
        log_store.add_log(f"Cache limpiado — {len(backup_ids)} IDs previos en backup", "info")
    except Exception as ex:
        log_store.add_log(f"Aviso limpiando cache: {ex}", "warning")

    try:
        log_store.add_log("Iniciando procesamiento de correos...", "info")
        app_module.process_emails()
    except Exception as e:
        import traceback
        log_store.add_log(f"Error en procesamiento: {str(e)}", "error")
        log_store.add_log(traceback.format_exc(), "error")
    finally:
        sys.stdout = old_stdout
        scheduler_state["running"] = False
        try:
            if os.path.exists(processed_path):
                with open(processed_path) as f:
                    new_ids = set(json.load(f))
                with open(processed_path, 'w') as f:
                    json.dump(list(backup_ids | new_ids), f)
        except Exception:
            pass


def _launch_process_thread():
    t = threading.Thread(target=run_process_with_logs, daemon=True, name="invoice-processor")
    t.start()


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
def _rebuild_schedule():
    schedule.clear()
    if not scheduler_state["enabled"]:
        return
    if scheduler_state["mode"] == "interval":
        mins = scheduler_state["interval_minutes"]
        schedule.every(mins).minutes.do(_launch_process_thread)
        log_store.add_log(f"Scheduler: cada {mins} minutos", "info")
    elif scheduler_state["mode"] == "daily":
        t = scheduler_state["daily_time"]
        schedule.every().day.at(t).do(_launch_process_thread)
        log_store.add_log(f"Scheduler: diario a las {t}", "info")


def _scheduler_loop(stop: threading.Event):
    while not stop.is_set():
        schedule.run_pending()
        jobs = schedule.get_jobs()
        if jobs:
            nxt = min(jobs, key=lambda j: j.next_run)
            scheduler_state["next_run"] = nxt.next_run.strftime("%H:%M %d/%m/%Y")
        else:
            scheduler_state["next_run"] = None
        stop.wait(timeout=10)


def _start_scheduler():
    global _scheduler_thread, _scheduler_stop
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, args=(_scheduler_stop,),
        daemon=True, name="scheduler"
    )
    _scheduler_thread.start()


_start_scheduler()


# ── LEER SHEETS ───────────────────────────────────────────────────────────────
def read_sheets_df():
    import pandas as pd
    try:
        ws = app_module.get_sheet()
        rows = ws.get_all_values()
        if not rows or len(rows) < 2:
            return None
        headers = rows[0]
        data = rows[1:]
        df = pd.DataFrame(data, columns=headers)
        df = df.replace("", pd.NA).dropna(how="all")
        return df
    except Exception:
        return None


# ── RUTAS API ──────────────────────────────────────────────────────────────────
@fastapi_app.get("/api/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@fastapi_app.get("/api/stats")
async def get_statistics():
    import pandas as pd
    try:
        df = read_sheets_df()
        if df is None or df.empty or "Estado" not in df.columns:
            return {"total": 0, "pendientes": 0, "pagadas": 0, "vencidas": 0,
                    "total_cop": 0.0, "total_usd": 0.0, "error": None}

        df["Total"] = pd.to_numeric(df["Total"], errors="coerce").fillna(0)
        moneda_col = next((c for c in df.columns if "moneda" in str(c).lower()), "Moneda")
        factura_col = next((c for c in df.columns if "factura" in str(c).lower()), None)

        return {
            "total":      int(df[factura_col].notna().sum()) if factura_col else len(df),
            "pendientes": int((df["Estado"] == "PENDIENTE").sum()),
            "pagadas":    int((df["Estado"] == "PAGADA").sum()),
            "vencidas":   int((df["Estado"] == "VENCIDA").sum()),
            "total_cop":  float(df.loc[df[moneda_col] == "COP", "Total"].sum()) if moneda_col in df.columns else 0.0,
            "total_usd":  float(df.loc[df[moneda_col] == "USD", "Total"].sum()) if moneda_col in df.columns else 0.0,
            "error": None,
        }
    except Exception as e:
        log_store.add_log(f"Error estadísticas: {e}", "error")
        return {"total": 0, "pendientes": 0, "pagadas": 0, "vencidas": 0,
                "total_cop": 0.0, "total_usd": 0.0, "error": str(e)}


@fastapi_app.get("/api/invoices")
async def get_invoices(limit: int = 50):
    try:
        df = read_sheets_df()
        if df is None or df.empty:
            return {"invoices": [], "total": 0}
        records = df.tail(limit).fillna("N/A").to_dict(orient="records")
        return {"invoices": records, "total": len(df)}
    except Exception as e:
        log_store.add_log(f"Error obteniendo facturas: {e}", "error")
        return {"invoices": [], "total": 0, "error": str(e)}


@fastapi_app.get("/api/invoices/by-status/{status}")
async def get_invoices_by_status(status: str):
    try:
        df = read_sheets_df()
        if df is None or "Estado" not in df.columns:
            return {"invoices": [], "count": 0}
        df_f = df[df["Estado"] == status.upper()].fillna("N/A")
        return {"status": status.upper(), "count": len(df_f),
                "invoices": df_f.to_dict(orient="records")}
    except Exception as e:
        return {"error": str(e), "invoices": []}


@fastapi_app.post("/api/process")
async def trigger_process():
    if scheduler_state["running"]:
        return {"status": "ya_ejecutando", "message": "Proceso activo, espera que termine"}
    log_store.add_log("Procesamiento manual iniciado desde dashboard", "info")
    _launch_process_thread()
    return {"status": "iniciado", "timestamp": datetime.now().isoformat()}


@fastapi_app.get("/api/logs")
async def get_logs():
    return {"logs": log_store.get_logs()}


@fastapi_app.post("/api/export-excel")
async def export_excel():
    try:
        path = app_module.export_to_excel()
        from fastapi.responses import FileResponse
        return FileResponse(path,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            filename="facturas_seguimiento.xlsx")
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=500, content={"error": str(e)})


@fastapi_app.delete("/api/logs")
async def clear_logs():
    log_store.clear()
    return {"status": "ok"}


@fastapi_app.get("/api/status")
async def get_status():
    return {
        "agente":         "activo",
        "almacenamiento": "google_sheets",
        "sheets_id":      app_module.GOOGLE_SHEETS_ID,
        "procesando":     scheduler_state["running"],
        "clientes_ws":    len(connected_clients),
        "timestamp":      datetime.now().isoformat(),
        "logs_totales":   len(log_store.get_logs()),
    }


@fastapi_app.get("/api/scheduler")
async def get_scheduler():
    return {**scheduler_state}


@fastapi_app.post("/api/scheduler")
async def set_scheduler(config: dict):
    if "enabled"          in config: scheduler_state["enabled"]          = bool(config["enabled"])
    if "mode"             in config: scheduler_state["mode"]             = config["mode"]
    if "interval_minutes" in config: scheduler_state["interval_minutes"] = max(5, int(config["interval_minutes"]))
    if "daily_time"       in config: scheduler_state["daily_time"]       = config["daily_time"]
    _rebuild_schedule()
    estado = "activado" if scheduler_state["enabled"] else "desactivado"
    log_store.add_log(f"Scheduler {estado}", "success" if scheduler_state["enabled"] else "warning")
    return {**scheduler_state}


# ── CHAT CON CLAUDE (Anthropic) ───────────────────────────────────────────────
@fastapi_app.post("/api/chat")
async def chat_with_agent(payload: dict):
    """
    Chat con Claude (Anthropic) usando contexto de facturas actuales.
    Recibe: { message, history, context }
    """
    try:
        import anthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            return {"response": "❌ Error: ANTHROPIC_API_KEY no configurada en .env"}

        client = anthropic.Anthropic(api_key=api_key)

        context = payload.get("context", {})
        history = payload.get("history", [])
        message = payload.get("message", "")

        system_prompt = f"""Eres un asistente financiero especializado en análisis de facturas del sistema Invoice Agent.
Tienes acceso al estado actual del sistema:

• Total de facturas registradas: {context.get('total', 'N/A')}
• Facturas pendientes de pago: {context.get('pendientes', 'N/A')}
• Facturas pagadas: {context.get('pagadas', 'N/A')}
• Facturas vencidas: {context.get('vencidas', 'N/A')}
• Total acumulado en COP: ${context.get('total_cop', 0):,.0f}
• Total acumulado en USD: ${context.get('total_usd', 0):,.2f}

El sistema extrae facturas automáticamente de Gmail usando Claude AI (Anthropic).
Los campos disponibles por factura son: Mes, Fecha Factura, Número Factura, Proveedor,
ID, Número ID, Subtotal, Descuento, IVA, Rete IVA, Rete ICA, Impto Consumo, Propina,
Otros Impuestos, Retención en la fuente, Administración, Utilidad, Imprevistos,
Valor Total, Clasificación, Estado, Valor Pagado, Valor Por Pagar, Fecha Pago,
Cliente, Cotización Inventto, Observaciones.

Responde siempre en español. Sé conciso y directo. Si te piden datos específicos
que no tienes disponibles, indícalo claramente y sugiere cómo obtenerlos."""

        # Construir historial de mensajes
        messages = []
        for h in history[-10:]:
            role = h.get("role", "user")
            if role not in ("user", "assistant"):
                continue
            messages.append({"role": role, "content": h["content"]})
        messages.append({"role": "user", "content": message})

        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=system_prompt,
            messages=messages
        )

        reply = resp.content[0].text.strip()
        return {"response": reply}

    except Exception as e:
        log_store.add_log(f"Error en chat: {str(e)}", "error")
        return {"response": f"Error al procesar la consulta: {str(e)}"}


# ── WEBSOCKET ─────────────────────────────────────────────────────────────────
@fastapi_app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    connected_clients.add(websocket)
    try:
        for log in log_store.get_logs():
            await websocket.send_json(log)
        last_count = len(log_store.get_logs())

        while True:
            await asyncio.sleep(1.5)
            try:
                await websocket.send_json({
                    "type": "ping", "message": "", "level": "ping",
                    "timestamp": datetime.now().strftime("%H:%M:%S")
                })
            except Exception:
                break
            logs = log_store.get_logs()
            for log in logs[last_count:]:
                try:
                    await websocket.send_json(log)
                except Exception:
                    break
            last_count = len(logs)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        connected_clients.discard(websocket)


# ── DASHBOARD ─────────────────────────────────────────────────────────────────
if os.path.exists("static"):
    fastapi_app.mount("/static", StaticFiles(directory="static"), name="static")


@fastapi_app.get("/dashboard.html", response_class=HTMLResponse)
async def get_dashboard():
    path = os.path.join(os.getcwd(), "dashboard.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"<h1>Error cargando dashboard: {e}</h1>"


@fastapi_app.get("/", response_class=HTMLResponse)
async def root():
    path = os.path.join(os.getcwd(), "dashboard.html")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return "<h1>Invoice Agent</h1><a href='/dashboard.html'>Dashboard</a> | <a href='/docs'>API Docs</a>"


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("  Dashboard : http://localhost:9000/dashboard.html")
    print("  API Docs  : http://localhost:9000/docs")
    uvicorn.run(fastapi_app, host="0.0.0.0", port=9000)
