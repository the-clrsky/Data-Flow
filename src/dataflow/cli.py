"""`data-flow` console command — launches the web app."""
import os

import uvicorn


def main():
    host = os.environ.get("DATAFLOW_HOST", "0.0.0.0")
    port = int(os.environ.get("DATAFLOW_PORT", "8000"))
    print(f"Data Flow  ->  http://localhost:{port}   (LAN: http://<this-host-ip>:{port})")
    uvicorn.run("dataflow.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
