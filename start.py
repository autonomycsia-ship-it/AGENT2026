[build]
builder = "NIXPACKS"
buildCommand = "pip install 'uvicorn[standard]' --force-reinstall --no-cache-dir && python -c 'import websockets; print(\"websockets OK\")' && python -c 'import uvloop; print(\"uvloop OK\")'"

[deploy]
startCommand = "python start.py"
healthcheckPath = "/api/health"
healthcheckTimeout = 30
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
