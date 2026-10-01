# -*- coding: utf-8 -*-
"""Arranca el servidor en un hilo y prueba REST y MCP con una clave. Uso: python prueba_local.py tv_live_…
Lee SUPABASE_SERVICE_KEY del entorno o de ../Tienda Tecnovinyl/CLAVE SUPABASE.txt."""
import os, sys, json, threading, time, asyncio
if not os.environ.get("SUPABASE_SERVICE_KEY"):
    for cand in ["../Tienda Tecnovinyl/CLAVE SUPABASE.txt", r"C:\Users\Administrador\Claude\Projects\Tienda Tecnovinyl\CLAVE SUPABASE.txt"]:
        if os.path.exists(cand):
            os.environ["SUPABASE_SERVICE_KEY"] = [l.strip() for l in open(cand, encoding="utf-8", errors="ignore") if l.strip() and not l.startswith("#")][0]; break
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
clave = sys.argv[1] if len(sys.argv) > 1 else ""
import uvicorn, httpx
from app import app
PORT = 8765
th = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning"), daemon=True); th.start()
base = f"http://127.0.0.1:{PORT}"
for _ in range(50):
    try: httpx.get(base + "/ping", timeout=2); break
    except Exception: time.sleep(0.2)
H = {"Authorization": "Bearer " + clave}
def ver(t, r): print(f"{t}: {r.status_code} {r.text[:260]}")
ver("ping", httpx.get(base + "/ping"))
ver("portada", httpx.get(base + "/"))
ver("openapi", httpx.get(base + "/openapi.json"))
ver("categorias", httpx.get(base + "/v1/categorias"))
r = httpx.get(base + "/v1/productos", params={"q": "pvc espumado blanco"}); ver("buscar", r)
slug = r.json()[0]["slug"]
r = httpx.get(base + f"/v1/productos/{slug}"); ver("ficha", r)
combos = r.json()["combinaciones"]
v = next(c for c in combos if c["combinacion"].get("Espesor") == "5 mm" and "entera" in c["combinacion"].get("Tamaño", "").lower())
print("variante elegida:", v)
if clave:
    ver("yo", httpx.get(base + "/v1/yo", headers=H))
    ver("precio", httpx.get(base + f"/v1/precio/variante/{v['variante_id']}", params={"cantidad": 3}, headers=H))
    rec = httpx.get(base + "/v1/productos", params={"q": "metacrilato incoloro recortes"}).json()[0]
    ver("medidas", httpx.post(base + "/v1/precio/medidas", json={"producto_id": rec["producto_id"], "ancho_mm": 600, "alto_mm": 400, "cantidad": 2}, headers=H))
    r = httpx.post(base + "/v1/pedidos", json={"lineas": [{"variante_id": v["variante_id"], "cantidad": 3}, {"producto_id": rec["producto_id"], "ancho_mm": 600, "alto_mm": 400, "cantidad": 2}], "entrega": {"modo": "recogida"}}, headers=H); ver("pedido", r)
    pid = r.json().get("pedido_id")
    ver("anular", httpx.post(base + f"/v1/pedidos/{pid}/anular", headers=H))
    ver("pedidos", httpx.get(base + "/v1/pedidos", params={"limite": 3}, headers=H))
    ver("sin clave", httpx.get(base + "/v1/yo"))
    async def mcp_test():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client(base + "/mcp/" + clave) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools(); print("MCP tools:", [t.name for t in tools.tools])
                res = await s.call_tool("quien_soy", {}); print("MCP quien_soy:", res.content[0].text[:200])
                res = await s.call_tool("buscar_productos", {"texto": "pvc 5 mm"}); print("MCP buscar:", res.content[0].text[:160])
                res = await s.call_tool("precio_combinacion", {"variante_id": v["variante_id"], "cantidad": 2}); print("MCP precio:", res.content[0].text[:200])
    asyncio.run(mcp_test())
print("fin")
