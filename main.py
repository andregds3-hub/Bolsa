"""Sobe o simulador em http://127.0.0.1:8000 (Shift+F10 no PyCharm)."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("api.app:app", host="127.0.0.1", port=8000, log_level="info")
