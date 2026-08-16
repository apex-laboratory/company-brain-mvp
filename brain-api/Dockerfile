FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default to the API server (mature app under app/). The compose file overrides
# `command` for the MCP server (app.mcp.server) and the ARQ worker
# (app.jobs.worker.WorkerSettings), which share this same image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
