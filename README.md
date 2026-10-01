# tecnovinyl-api

API pública y servidor MCP de la tienda profesional de Tecnovinyl. FastAPI + Supabase.

- `app.py`: endpoints REST (`/v1/...`), documentación (`/`, `/docs`, `/openapi.json`, `/llms.txt`) y servidor MCP (`/mcp/<clave>`).
- La lógica de precios y pedidos está en la base de datos (`supabase/carga/31-api.sql` del proyecto Tienda Tecnovinyl): este servidor solo valida la clave y traduce.
- Variables: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (secreta), `API_URL`.
- Local: `pip install -r requirements.txt` y `uvicorn app:app --port 8080`. Pruebas: `python prueba_local.py <clave>`.
- Producción: Railway, servicio `tecnovinyl-api` (deploy automático desde `main`), dominio `api.tecnovinyl.com`.
