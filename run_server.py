"""Local dev runner: python run_server.py [port]"""
import os
import uvicorn
from app.main import app  # noqa: F401  (import registers routes/lifespan)

if __name__ == "__main__":
    port = int(sys.argv[1]) if (sys := __import__("sys")).argv[1:] else int(os.environ.get("PORT", "4680"))
    uvicorn.run(app, host="0.0.0.0", port=port)
