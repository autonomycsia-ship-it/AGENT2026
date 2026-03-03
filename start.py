"""
Entry point — arranca el servidor FastAPI.
Railway inyecta la variable PORT automaticamente.
"""
import uvicorn
import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9000))
    print(f"  Dashboard : http://localhost:{port}/dashboard.html")
    print(f"  API Docs  : http://localhost:{port}/docs")
    uvicorn.run(
        "backend:fastapi_app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
        ws="websockets",
    )
