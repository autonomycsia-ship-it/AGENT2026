import uvicorn
import os

if __name__ == "__main__":
    # Railway inyecta PORT automaticamente
    # En local usa 9000 como fallback
    port = int(os.environ.get("PORT", 9000))
    print(f"Dashboard : http://localhost:{port}/dashboard.html")
    print(f"API Docs  : http://localhost:{port}/docs")
    uvicorn.run(
        "backend:fastapi_app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )
```

---

## Y el `Procfile` — créalo en GitHub
```
1. En tu repo → botón "Add file" → "Create new file"
2. Nombre del archivo: Procfile  (sin extensión, con P mayúscula)
3. Contenido:
```
```
uvicorn.run(fastapi_app, host="0.0.0.0", port=9000, ws="websockets", loop="uvloop")
```
```
4. Commit changes
