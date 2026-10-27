# -*- coding: utf-8 -*-
"""tecnovinyl-api · API pública y servidor MCP de la tienda de Tecnovinyl (01/10/2026).

Un cliente validado recibe una clave (tv_live_…) desde el panel y con ella su agente o su software
puede consultar el catálogo, pedir precios con SU tarifa y crear pedidos. Los pedidos nacen
«pendientes de confirmación» y se confirman en una segunda llamada, con el total a la vista.

Toda la lógica de precios y de pedidos vive en la base de datos (funciones api_* de 31-api.sql);
este servidor solo valida, traduce y documenta. Variables: SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""
import os, re, json, time, asyncio
from typing import Any, Optional
import httpx
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
import bandejas as BJ
import fabricados as FB
import interno as INT   # conector interno para Claude Team (27/10/2026): /interno/mcp con login del panel

SB = os.environ.get("SUPABASE_URL", "https://vqfcfbqlycjgqmdfympr.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TIENDA = "https://www.tecnovinyl.com/tienda/"
API_URL = os.environ.get("API_URL", "https://api.tecnovinyl.com")
VERSION = "1.3"

if not KEY:
    print("AVISO: falta SUPABASE_SERVICE_KEY; las llamadas a la base de datos fallarán")

_http = httpx.AsyncClient(base_url=SB + "/rest/v1", timeout=40,
                          headers={"apikey": KEY, "Authorization": "Bearer " + KEY})


class ApiError(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


async def rest(path: str, params: Optional[dict] = None) -> Any:
    r = await _http.get(path, params=params)
    if r.status_code >= 400:
        raise ApiError(502, "error de catálogo: " + r.text[:200])
    return r.json()


async def rpc(fn: str, body: dict) -> Any:
    r = await _http.post("/rpc/" + fn, json=body, headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except Exception:
            msg = r.text
        code = 401 if r.status_code == 403 and "clave" in (msg or "") else 400
        raise ApiError(code, msg[:300])
    return r.json()


def clave_de(authorization: Optional[str], x_api_key: Optional[str]) -> str:
    k = (x_api_key or "").strip()
    if not k and authorization:
        m = re.match(r"^\s*Bearer\s+(.+)$", authorization, re.I)
        if m:
            k = m.group(1).strip()
    if not k or not re.match(r"^tv_[a-z]+_[0-9a-f]{48}$", k):
        raise ApiError(401, "Falta la clave de API (cabecera Authorization: Bearer tv_live_…)")
    return k


# ----------------------------------------------------------------------------- catálogo (sin precios)
_cache: dict = {}


async def cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    v = await fn()
    _cache[key] = (now + ttl, v)
    return v


async def categorias() -> list:
    async def f():
        cats = await rest("/categorias", {"select": "id,nombre,padre_id,orden", "order": "orden,nombre"})
        n = await rest("/productos_publicos", {"select": "categoria_id"})
        cnt: dict = {}
        for p in n:
            cnt[p["categoria_id"]] = cnt.get(p["categoria_id"], 0) + 1
        return [dict(c, productos=cnt.get(c["id"], 0)) for c in cats]
    return await cached("cats", 300, f)


def norm(s: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(ch) != "Mn")


async def lista_productos() -> list:
    async def f():
        return await rest("/productos_publicos", {
            "select": "id,slug,nombre,tipo,categoria_id,marca,descripcion_corta,unidad_venta,bajo_pedido,"
                      "variantes_publicas(referencia_ahora,activa)",
            "order": "nombre"})
    return await cached("prods", 120, f)


def descendientes(cats: list, cid: str) -> set:
    out, pend = set(), [cid]
    while pend:
        c = pend.pop()
        if c in out:
            continue
        out.add(c)
        pend += [x["id"] for x in cats if x["padre_id"] == c]
    return out


async def buscar(q: str = "", categoria: str = "", limite: int = 50) -> list:
    prods, cats = await lista_productos(), await categorias()
    nombre_cat = {c["id"]: c["nombre"] for c in cats}
    sel = prods
    if categoria:
        ids = descendientes(cats, categoria)
        sel = [p for p in sel if p["categoria_id"] in ids]
    if q:
        t = norm(q).split()
        def ok(p):
            texto = norm(" ".join([p["nombre"], p.get("descripcion_corta") or "", p.get("marca") or "",
                                   nombre_cat.get(p["categoria_id"], ""),
                                   " ".join((v.get("referencia_ahora") or "") for v in (p.get("variantes_publicas") or []))]))
            return all(w in texto for w in t)
        sel = [p for p in sel if ok(p)]
        # primero los que llevan todas las palabras en el nombre, luego por nombre
        sel.sort(key=lambda p: (0 if all(w in norm(p["nombre"]) for w in t) else 1, norm(p["nombre"])))
    out = []
    # bandejas de composite (23/10/2026): en la tienda son configuradores, aquí productos virtuales
    for v in BJ.VIRTUALES:
        if categoria and v["categoria_id"] not in descendientes(cats, categoria):
            continue
        if q and not all(w in norm(v["nombre"] + " " + v["descripcion_corta"] + " " + v["categoria"]) for w in norm(q).split()):
            continue
        out.append(dict(v))
    for p in sel[:max(1, min(limite, 200))]:
        out.append({"producto_id": p["id"], "slug": p["slug"], "nombre": p["nombre"], "tipo": p["tipo"],
                    "categoria": nombre_cat.get(p["categoria_id"]), "categoria_id": p["categoria_id"],
                    "descripcion_corta": p.get("descripcion_corta"), "bajo_pedido": p.get("bajo_pedido"),
                    "url": TIENDA + "#/p/" + p["slug"]})
    return out


TIPOS = {"variantes": "Producto con combinaciones (color, medida…): pide el precio y haz el pedido con variante_id.",
         "simple": "Producto único: tiene una sola combinación (variante_id).",
         "medidas": "Producto a medida: pásale ancho_mm y alto_mm (y las opciones) a /v1/precio/medidas y al pedido.",
         "tabla": "Producto por tabla de opciones: elige las opciones y usa /v1/precio/medidas.",
         "dxf": "Producto fabricado a partir de un archivo DXF: por API solo se puede consultar; el pedido se hace desde la tienda subiendo el archivo."}


async def ficha(id_o_slug: str) -> dict:
    v = BJ.es_virtual(id_o_slug)
    if v:
        return BJ.ficha_virtual(v)
    filtro = {"id": "eq." + id_o_slug} if re.match(r"^[0-9a-f-]{36}$", id_o_slug) else {"slug": "eq." + id_o_slug}
    r = await rest("/productos_publicos", dict(filtro, **{
        "select": "id,slug,nombre,tipo,categoria_id,marca,descripcion_corta,descripcion,unidad_venta,limites,plazo,bajo_pedido,"
                  "atributos(id,nombre,orden,atributo_valores(id,valor,codigo,orden)),"
                  "variantes_publicas(id,valores,activa,referencia_ahora),"
                  "caracteristicas(id,nombre,magnitud,oculta,ayuda,orden,caracteristica_valores(id,nombre,orden,por_defecto))"}))
    if not r:
        raise ApiError(404, "producto no encontrado")
    p = r[0]
    cats = await categorias()
    nombre_cat = {c["id"]: c["nombre"] for c in cats}
    vals = {}
    atributos = []
    for a in sorted(p.get("atributos") or [], key=lambda a: a["orden"]):
        vs = sorted(a.get("atributo_valores") or [], key=lambda v: v["orden"])
        for v in vs:
            vals[v["id"]] = (a["nombre"], v["valor"])
        atributos.append({"nombre": a["nombre"], "valores": [{"valor": v["valor"], "codigo": v.get("codigo")} for v in vs]})
    combinaciones = []
    for v in p.get("variantes_publicas") or []:
        if not v.get("activa"):
            continue
        combo = {vals[x][0]: vals[x][1] for x in (v.get("valores") or []) if x in vals}
        combinaciones.append({"variante_id": v["id"], "combinacion": combo, "referencia": v.get("referencia_ahora")})
    opciones = []
    for c in sorted(p.get("caracteristicas") or [], key=lambda c: c["orden"]):
        if c.get("oculta"):
            continue
        cv = sorted(c.get("caracteristica_valores") or [], key=lambda x: x["orden"])
        if len(cv) <= 1:
            continue
        opciones.append({"nombre": c["nombre"], "ayuda": c.get("ayuda"),
                         "valores": [x["nombre"] for x in cv],
                         "por_defecto": next((x["nombre"] for x in cv if x.get("por_defecto")), cv[0]["nombre"])})
    return {"producto_id": p["id"], "slug": p["slug"], "nombre": p["nombre"], "tipo": p["tipo"], "como_se_pide": TIPOS.get(p["tipo"]),
            "categoria": nombre_cat.get(p["categoria_id"]), "categoria_id": p["categoria_id"], "marca": p.get("marca"),
            "descripcion_corta": p.get("descripcion_corta"), "descripcion": p.get("descripcion"),
            "unidad_venta": p.get("unidad_venta"), "plazo": p.get("plazo"), "bajo_pedido": p.get("bajo_pedido"),
            "limites_mm": p.get("limites"), "atributos": atributos, "combinaciones": combinaciones,
            "opciones": opciones, "url": TIENDA + "#/p/" + p["slug"]}


def magnitudes_de(ancho_mm: Optional[float], alto_mm: Optional[float]) -> dict:
    a, h = float(ancho_mm or 0), float(alto_mm or 0)
    m2 = a * h / 1e6
    return {"m2_ancho_alto": round(m2, 4), "perimetro": round(2 * (a + h) / 1000, 4), "m2_pieza": round(m2, 4), "n_piezas": 1}


def eur(c) -> str:
    try:
        return f"{int(c) / 100:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "—"


# ----------------------------------------------------------------------------- operaciones con clave
async def yo(clave: str) -> dict:
    return await rpc("api_cliente", {"p_clave": clave})


async def precio_variante(clave: str, variante_id: str, cantidad: float = 1) -> dict:
    r = await rpc("api_precio_variante", {"p_clave": clave, "p_variante": variante_id, "p_cantidad": cantidad})
    r["unitario"] = eur(r.get("unitario_cent")); r["total_sin_iva"] = eur(r.get("total_cent"))
    return r


async def precio_medidas(clave: str, producto_id: str, seleccion: dict, ancho_mm=None, alto_mm=None, magnitudes=None, cantidad: int = 1) -> dict:
    mags = magnitudes or magnitudes_de(ancho_mm, alto_mm)
    r = await rpc("api_precio_medidas", {"p_clave": clave, "p_producto": producto_id, "p_seleccion": seleccion or {},
                                         "p_magnitudes": mags, "p_cantidad": cantidad})
    r["magnitudes"] = mags
    r["unitario"] = eur(r.get("unitario_cent")); r["total_sin_iva"] = eur(r.get("total_cent"))
    return r


def _conf(fn, d):
    try:
        return fn(d)
    except BJ.ErrorBandeja as e:
        raise ApiError(400, str(e))


async def precio_bandeja(clave: str, tipo: str, d: dict) -> dict:
    conf = _conf(BJ.conf_ciega if tipo == "ciega" else BJ.conf_esquina, d)
    r = await rpc("api_precio_bandeja", {"p_clave": clave, "p_tipo": tipo, "p": conf})
    if r.get("error"):
        raise ApiError(400, r["error"] + (" Hay que estudiarla con Tecnovinyl: pídela como presupuesto en la tienda." if r.get("juntas") else ""))
    out = {"precio_cent": r.get("precio_cent"), "pvp_cent": r.get("pvp_cent"), "descuento_pct": round(float(r.get("dto") or 0) * 100),
           "unitario_cent": r.get("unitario_cent"), "cantidad": r.get("q"),
           "precio": eur(r.get("precio_cent")) + " + IVA", "pvp_tachado": eur(r.get("pvp_cent"))}
    if tipo == "ciega":
        out["piezas"] = r.get("n")
        if (r.get("n") or 1) > 1:
            out["nota"] = f"Por su largo va en {r['n']} piezas de {r['lp']} × {r['c']} mm a tope sobre una sola estructura."
    else:
        out.update({"desarrollo_mm": conf["desarrollo"], "bandejas": len(conf["piezas"]), "despiece": BJ.despiece_legible(conf),
                    "nota": "Una sola estructura de aluminio en todo el desarrollo, con inglete en las esquinas."})
    out["linea_para_pedido"] = {("bandeja_ciega" if tipo == "ciega" else "bandejas_esquina"): d,
                                "cantidad": (conf.get("q") or 1)}
    return out


async def precio_fabricado(clave: str, tipo: str, d: dict) -> dict:
    """Letras corpóreas y neón por costes (26/10/2026): mismo cálculo que la tienda; solo precio."""
    if tipo not in FB.TIPOS:
        raise ApiError(400, "tipo no válido: " + ", ".join(FB.TIPOS))
    try:
        conf, estimado = FB.conf(tipo, d)
    except FB.ErrorFab as e:
        raise ApiError(400, str(e))
    r = await rpc("api_precio_fabricado", {"p_clave": clave, "p_tipo": tipo, "p": conf})
    if r.get("error"):
        raise ApiError(400, r["error"])
    precio_keys = {"precio_cent", "pvp_cent", "unitario_cent", "pvp_unitario_cent", "dto", "q"}
    out = {"producto": FB.TIPOS[tipo][1], "precio_cent": r.get("precio_cent"), "pvp_cent": r.get("pvp_cent"),
           "descuento_pct": round(float(r.get("dto") or 0) * 100), "unitario_cent": r.get("unitario_cent"), "cantidad": r.get("q"),
           "precio": eur(r.get("precio_cent")) + " + IVA", "pvp_tachado": eur(r.get("pvp_cent")),
           "detalle": {k: v for k, v in r.items() if k not in precio_keys and v is not None},
           "estimado": estimado,
           "como_pedirlo": ("Se pide en la tienda con el simulador de neón: " if tipo == "neon" else "Se pide en la tienda subiendo el DXF a escala real: ") + FB.url_de(tipo)}
    if estimado:
        out["nota"] = ("Precio ORIENTATIVO: " + ("faltan los metros de neón y las piezas exactas (los da el simulador de la tienda)."
                       if tipo == "neon" else "se ha estimado la forma de las letras; con el DXF (superficie y contorno de cada pieza) sale el precio exacto."))
    return out


def normaliza_lineas(lineas: list) -> list:
    out = []
    for l in lineas or []:
        l = dict(l)
        if l.get("bandeja_ciega"):
            conf = _conf(BJ.conf_ciega, dict(l["bandeja_ciega"], cantidad=l.get("cantidad") or l["bandeja_ciega"].get("cantidad") or 1))
            out.append({"producto_id": None, "cantidad": conf["q"], "bandeja_ciega": conf})
        elif l.get("bandejas_esquina") or l.get("bandeja_esquina"):
            conf = _conf(BJ.conf_esquina, l.get("bandejas_esquina") or l.get("bandeja_esquina"))
            out.append({"producto_id": None, "cantidad": 1, "bandeja_esquina": conf})
        elif l.get("variante_id"):
            out.append({"variante_id": l["variante_id"], "cantidad": l.get("cantidad", 1), "instrucciones": l.get("instrucciones")})
        elif l.get("producto_id"):
            mags = l.get("magnitudes") or magnitudes_de(l.get("ancho_mm"), l.get("alto_mm"))
            out.append({"producto_id": l["producto_id"], "seleccion": l.get("seleccion") or {}, "magnitudes": mags,
                        "cantidad": l.get("cantidad", 1), "instrucciones": l.get("instrucciones")})
        else:
            raise ApiError(400, "cada línea necesita variante_id, producto_id, bandeja_ciega o bandejas_esquina")
    if not out:
        raise ApiError(400, "el pedido no tiene líneas")
    return out


async def crear_pedido(clave: str, lineas: list, entrega: Optional[dict], confirmar: bool = False) -> dict:
    r = await rpc("api_crear_pedido", {"p_clave": clave, "p_lineas": normaliza_lineas(lineas), "p_entrega": entrega or {}, "p_confirmar": bool(confirmar)})
    r["total_con_iva"] = eur(r.get("total_cent")); r["subtotal_sin_iva"] = eur(r.get("subtotal_cent"))
    r["siguiente_paso"] = ("Pedido confirmado: Tecnovinyl lo tiene ya." if r.get("estado") == "confirmado"
                           else "Pedido pendiente: revisa el total con el cliente y confírmalo con POST /v1/pedidos/{pedido_id}/confirmar (o anúlalo).")
    return r


# ----------------------------------------------------------------------------- FastAPI
app = FastAPI(title="Tecnovinyl · API de la tienda", version=VERSION,
              description="API para que los clientes profesionales de Tecnovinyl (y sus agentes de IA) consulten el catálogo, "
                          "obtengan precios con su tarifa y hagan pedidos. Autenticación: `Authorization: Bearer tv_live_…` "
                          "(la clave la da Tecnovinyl desde el panel). Los importes van en céntimos de euro, sin IVA salvo que se indique.",
              docs_url="/docs", redoc_url=None)


@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return JSONResponse({"error": e.msg}, status_code=e.status)


class LineaPedido(BaseModel):
    variante_id: Optional[str] = Field(None, description="Combinación concreta (productos con variantes o simples)")
    producto_id: Optional[str] = Field(None, description="Producto a medida o por tabla (sin variante_id)")
    cantidad: float = 1
    seleccion: Optional[dict] = Field(None, description="Opciones elegidas {nombre_opcion: valor} en productos a medida/tabla")
    ancho_mm: Optional[float] = None
    alto_mm: Optional[float] = None
    magnitudes: Optional[dict] = Field(None, description="Magnitudes ya calculadas (avanzado); si no, se calculan de ancho_mm × alto_mm")
    instrucciones: Optional[str] = None
    bandeja_ciega: Optional[dict] = Field(None, description="Bandeja ciega: los mismos parámetros que /v1/precio/bandeja-ciega")
    bandejas_esquina: Optional[dict] = Field(None, description="Bandejas en esquina: los mismos parámetros que /v1/precio/bandejas-esquina")


class BandejaCiegaIn(BaseModel):
    ancho_mm: float = Field(..., description="Ancho exterior en mm (mín. 300)")
    alto_mm: float = Field(..., description="Alto exterior en mm (mín. 300)")
    fondo_mm: float = Field(..., description="Fondo o canto en mm (20–300)")
    color: str = Field(..., description="negro-mate | negro-brillo | blanco-mate | blanco-brillo")
    esquinas: str = Field("v", description="v (en V) | recta")
    trasera: str = Field("sin", description="sin | estructura | contra")
    acabado: str = Field("natural", description="natural | ral")
    ral: Optional[str] = None
    observaciones: Optional[str] = None
    cantidad: int = 1


class LetrasIn(BaseModel):
    tipo: str = Field(..., description="letras_aluminio | letras_pvc_retro | letras_pvc_frontal | letras_metacrilato | letras_alupanel | letras_pvc")
    texto: Optional[str] = Field(None, description="Texto del rótulo (con alto_mm): precio orientativo")
    alto_mm: Optional[float] = Field(None, description="Alto de las letras en mm (con texto)")
    piezas: Optional[list[dict]] = Field(None, description="Cada letra del DXF: {ancho_mm, alto_mm, area_mm2, contorno_mm}; con superficie y contorno el precio es el exacto")
    opciones: Optional[dict] = Field(None, description="Opciones del tipo (ver GET /v1/precio/letras/opciones)")
    cantidad: int = 1


class NeonIn(BaseModel):
    ancho_mm: float = Field(..., description="Ancho del rótulo con la base, en mm")
    alto_mm: float = Field(..., description="Alto del rótulo con la base, en mm")
    metros_neon: Optional[float] = Field(None, description="Metros de neón (los da el simulador); si no, se estiman")
    piezas: Optional[int] = Field(None, description="Piezas de neón; si no, se estiman")
    color: str = Field("rojo", description="rojo | blanco | blanco_calido | violeta | rosa | naranja | amarillo | azul | turquesa | verde | rgb (exterior: blanco, blanco_calido, rojo, violeta, verde o rgb)")
    ambiente: str = Field("interior", description="interior | exterior")
    fijacion: str = Field("atornillado", description="atornillado | colgado | autoportante")
    base: str = Field("metacrilato_10", description="metacrilato_10 | metacrilato_6 | metacrilato_5 | pvc_19")
    cantidad: int = 1


class EsquinaIn(BaseModel):
    tipo: str = Field("saliente", description="saliente (hacia la calle) | entrante (rincón)")
    angulo: float = Field(90, description="Ángulo entre las dos paredes en grados; 90 = esquina normal")


class BandejasEsquinaIn(BaseModel):
    tramos_mm: list[float] = Field(..., description="Largo de cada tramo de fachada, en orden")
    esquinas: list[EsquinaIn] = Field(default_factory=list, description="Una menos que tramos")
    alto_mm: float = Field(..., description="Alto del rótulo (200–1500)")
    fondo_mm: float = Field(..., description="Fondo o canto (20–300)")
    color: str = Field(..., description="negro-mate | negro-brillo | blanco-mate | blanco-brillo")
    medidas_de: str = Field("pared", description="pared | rotulo")
    acabado: str = Field("natural", description="natural (color de la plancha) | ral (composite blanco pintado)")
    ral: Optional[str] = None
    observaciones: Optional[str] = None


class PedidoIn(BaseModel):
    lineas: list[LineaPedido]
    entrega: Optional[dict] = Field(None, description='Ej. {"modo": "recogida"} o {"modo": "envio", "direccion": "…", "observaciones": "…"}')
    confirmar: bool = Field(False, description="true = confirmar directamente; false = queda pendiente hasta POST /confirmar")


class PrecioMedidasIn(BaseModel):
    producto_id: str
    seleccion: Optional[dict] = None
    ancho_mm: Optional[float] = None
    alto_mm: Optional[float] = None
    magnitudes: Optional[dict] = None
    cantidad: int = 1


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def portada():
    with open(os.path.join(os.path.dirname(__file__), "docs.html"), encoding="utf-8") as f:
        return f.read().replace("{{API_URL}}", API_URL)


@app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
async def llms():
    with open(os.path.join(os.path.dirname(__file__), "llms.txt"), encoding="utf-8") as f:
        return f.read().replace("{{API_URL}}", API_URL)


@app.get("/ping", include_in_schema=False)
async def ping():
    return {"ok": True, "version": VERSION}


@app.get("/v1/yo", summary="Quién soy (comprueba la clave)")
async def v_yo(authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await yo(clave_de(authorization, x_api_key))


@app.get("/v1/categorias", summary="Categorías del catálogo")
async def v_categorias():
    return await categorias()


@app.get("/v1/productos", summary="Buscar productos (sin precios)")
async def v_productos(q: str = Query("", description="texto: nombre, referencia, marca…"), categoria: str = Query("", description="id de categoría"), limite: int = 50):
    return await buscar(q, categoria, limite)


@app.get("/v1/productos/{id_o_slug}", summary="Ficha de un producto: combinaciones y opciones")
async def v_producto(id_o_slug: str):
    return await ficha(id_o_slug)


@app.get("/v1/precio/variante/{variante_id}", summary="Precio de una combinación con tu tarifa")
async def v_precio_variante(variante_id: str, cantidad: float = 1, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await precio_variante(clave_de(authorization, x_api_key), variante_id, cantidad)


@app.post("/v1/precio/medidas", summary="Precio de un producto a medida o por tabla")
async def v_precio_medidas(b: PrecioMedidasIn, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await precio_medidas(clave_de(authorization, x_api_key), b.producto_id, b.seleccion or {}, b.ancho_mm, b.alto_mm, b.magnitudes, b.cantidad)


@app.post("/v1/precio/bandeja-ciega", summary="Precio de una bandeja ciega de composite a medida")
async def v_precio_ciega(b: BandejaCiegaIn, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await precio_bandeja(clave_de(authorization, x_api_key), "ciega", b.model_dump())


@app.post("/v1/precio/bandejas-esquina", summary="Despiece y precio de un rótulo de bandejas en fachada con esquinas")
async def v_precio_esquina(b: BandejasEsquinaIn, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await precio_bandeja(clave_de(authorization, x_api_key), "esquina", b.model_dump())


@app.get("/v1/precio/letras/opciones", summary="Tipos de letras corpóreas y sus opciones")
async def v_opciones_letras():
    return {t: {"producto": FB.TIPOS[t][1], "url": FB.url_de(t), "opciones": o} for t, o in FB.OPCIONES.items()}


@app.post("/v1/precio/letras", summary="Precio de letras corpóreas (aluminio, PVC, metacrilato, alupanel)")
async def v_precio_letras(b: LetrasIn, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await precio_fabricado(clave_de(authorization, x_api_key), b.tipo, b.model_dump())


@app.post("/v1/precio/neon", summary="Precio de un neón a medida")
async def v_precio_neon(b: NeonIn, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await precio_fabricado(clave_de(authorization, x_api_key), "neon", b.model_dump())


@app.get("/v1/pedidos", summary="Mis pedidos")
async def v_pedidos(limite: int = 50, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await rpc("api_pedidos", {"p_clave": clave_de(authorization, x_api_key), "p_limite": limite})


@app.post("/v1/pedidos", summary="Crear un pedido (pendiente de confirmar salvo confirmar=true)")
async def v_crear_pedido(b: PedidoIn, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await crear_pedido(clave_de(authorization, x_api_key), [l.model_dump(exclude_none=True) for l in b.lineas], b.entrega, b.confirmar)


@app.post("/v1/pedidos/{pedido_id}/confirmar", summary="Confirmar un pedido pendiente")
async def v_confirmar(pedido_id: str, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await rpc("api_confirmar_pedido", {"p_clave": clave_de(authorization, x_api_key), "p_pedido": pedido_id})


@app.post("/v1/pedidos/{pedido_id}/anular", summary="Anular un pedido pendiente (no confirmado)")
async def v_anular(pedido_id: str, authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None)):
    return await rpc("api_cancelar_pedido", {"p_clave": clave_de(authorization, x_api_key), "p_pedido": pedido_id})


# ----------------------------------------------------------------------------- MCP
mcp = FastMCP("Tecnovinyl", instructions=(
    "Tienda profesional de Tecnovinyl (materiales de rotulación, perfilería, LED, fabricación a medida). "
    "Trabajas en nombre de un cliente validado. Flujo: buscar_productos → ficha_producto → precio → crear_pedido "
    "(queda pendiente) → enseñar el total al usuario → confirmar_pedido. Nunca confirmes sin que el usuario lo apruebe. "
    "Bandejas de composite: precio_bandeja_ciega y precio_bandejas_esquina dan el precio (y el despiece) y la línea lista para el pedido; "
    "pregunta siempre el color, no lo supongas. "
    "Letras corpóreas y neón: precio_letras y precio_neon dan el precio con el mismo cálculo que la tienda (orientativo si solo hay "
    "texto y alto; exacto con las medidas de cada pieza del DXF o los metros del simulador de neón). "
    "Los productos por archivo DXF (letras, neón, bandeja calada…) no se pueden pedir por aquí: indícale al usuario que lo haga en " + TIENDA),
    stateless_http=True, json_response=True, streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


def clave_ctx(ctx: Context) -> str:
    req = ctx.request_context.request
    k = None
    if req is not None:
        k = (req.scope.get("state") or {}).get("clave") or req.headers.get("x-api-key")
        if not k:
            auth = req.headers.get("authorization") or ""
            m = re.match(r"^\s*Bearer\s+(.+)$", auth, re.I)
            k = m.group(1).strip() if m else None
    if not k:
        raise ApiError(401, "falta la clave de API: usa la URL /mcp/<clave> o la cabecera Authorization")
    return k


@mcp.tool()
async def quien_soy(ctx: Context) -> dict:
    """Comprueba la clave y devuelve el cliente en cuyo nombre se trabaja."""
    return await yo(clave_ctx(ctx))


@mcp.tool()
async def categorias_catalogo() -> list:
    """Lista las categorías del catálogo con su id (para filtrar búsquedas)."""
    return await categorias()


@mcp.tool()
async def buscar_productos(texto: str = "", categoria_id: str = "", limite: int = 30) -> list:
    """Busca productos por texto (nombre, referencia, marca) y/o categoría. Devuelve producto_id, slug, tipo y descripción; sin precios."""
    return await buscar(texto, categoria_id, limite)


@mcp.tool()
async def ficha_producto(producto: str) -> dict:
    """Ficha completa de un producto (por producto_id o slug): descripción, atributos, combinaciones (variante_id) y opciones a medida, y cómo se pide según su tipo."""
    return await ficha(producto)


@mcp.tool()
async def precio_combinacion(ctx: Context, variante_id: str, cantidad: float = 1) -> dict:
    """Precio con la tarifa del cliente de una combinación concreta (variante_id de la ficha). Importes en céntimos sin IVA."""
    return await precio_variante(clave_ctx(ctx), variante_id, cantidad)


@mcp.tool()
async def precio_a_medida(ctx: Context, producto_id: str, ancho_mm: float = 0, alto_mm: float = 0, seleccion: Optional[dict] = None, cantidad: int = 1) -> dict:
    """Precio de un producto a medida (tipo 'medidas') o por tabla (tipo 'tabla'): ancho y alto en mm y las opciones elegidas {nombre: valor}."""
    return await precio_medidas(clave_ctx(ctx), producto_id, seleccion or {}, ancho_mm, alto_mm, None, cantidad)


@mcp.tool()
async def precio_bandeja_ciega(ctx: Context, ancho_mm: float, alto_mm: float, fondo_mm: float, color: str, esquinas: str = "v",
                               trasera: str = "sin", acabado: str = "natural", ral: str = "", observaciones: str = "", cantidad: int = 1) -> dict:
    """Precio de una bandeja ciega de composite de 3 mm a medida (cálculo por costes de Tecnovinyl).
    color: negro-mate | negro-brillo | blanco-mate | blanco-brillo (pregúntalo). esquinas: v | recta.
    trasera: sin | estructura (aluminio) | contra (contrabandeja). acabado: natural | ral (con ral).
    Devuelve el precio sin IVA y linea_para_pedido, que se pasa tal cual a crear_pedido_pendiente."""
    return await precio_bandeja(clave_ctx(ctx), "ciega", dict(ancho_mm=ancho_mm, alto_mm=alto_mm, fondo_mm=fondo_mm, color=color, esquinas=esquinas,
                                trasera=trasera, acabado=acabado, ral=ral, observaciones=observaciones, cantidad=cantidad))


@mcp.tool()
async def precio_bandejas_esquina(ctx: Context, tramos_mm: list[float], esquinas: list[dict], alto_mm: float, fondo_mm: float, color: str = "",
                                  medidas_de: str = "pared", acabado: str = "natural", ral: str = "", observaciones: str = "") -> dict:
    """Despiece y precio de un rótulo de bandejas de composite en una fachada con esquinas, sobre estructura de aluminio con inglete.
    tramos_mm: largo de cada tramo en orden. esquinas: una menos que tramos, [{"tipo": "saliente"|"entrante", "angulo": 90}]
    (ángulo entre las dos paredes). medidas_de: pared | rotulo. color: negro-mate | negro-brillo | blanco-mate | blanco-brillo (pregúntalo),
    o acabado="ral" con ral="7016" para pintarla de otro color (se hace en composite blanco pintado).
    Devuelve el despiece (bandejas y pliegues), el precio sin IVA y linea_para_pedido para crear_pedido_pendiente."""
    return await precio_bandeja(clave_ctx(ctx), "esquina", dict(tramos_mm=tramos_mm, esquinas=esquinas, alto_mm=alto_mm, fondo_mm=fondo_mm,
                                color=color, medidas_de=medidas_de, acabado=acabado, ral=ral, observaciones=observaciones))


@mcp.tool()
async def precio_letras(ctx: Context, tipo: str, texto: str = "", alto_mm: float = 0, piezas: Optional[list[dict]] = None,
                        opciones: Optional[dict] = None, cantidad: int = 1) -> dict:
    """Precio de letras corpóreas fabricadas por Tecnovinyl (cálculo por costes, el mismo de la tienda).
    tipo: letras_aluminio | letras_pvc_retro | letras_pvc_frontal | letras_metacrilato | letras_alupanel | letras_pvc.
    Da texto + alto_mm (precio orientativo) o piezas [{ancho_mm, alto_mm, area_mm2, contorno_mm}] de cada letra del DXF (exacto).
    opciones según el tipo, por ejemplo:
    letras_aluminio {modelo: retro_difusor|retro|zocalo|frontal|sinluz_varillas|sinluz_pared, fondo, luz: frio|calido|rgb, ral};
    letras_pvc_retro {grosor: 19|30, material: blanco|negro, luz: sin|frio|calido|rgb, difusor: sin|opal|hielo, acabado: sin|ral, ral,
    fijacion: varilla|chupetes|sin, montaje: pared|bandeja, bandeja: {w,h,d,color,trasera}};
    letras_pvc_frontal {luz, frontal: opal|opal20|hielo, acabado, ral, montaje, bandeja};
    letras_metacrilato {material: incoloro|opal|hielo, grosor: 3|5|8|10, corte: fresadora|laser, fijacion: sin|cinta};
    letras_alupanel {color: blanco|negro, cara: brillo|mate, acabado: sin|ral, ral, fijacion: sin|cinta};
    letras_pvc {color: blanco|negro, grosor: 3|5|10|19|30, acabado: sin|ral, ral, fijacion: sin|cinta}.
    Letras sin luz: 20 mm de alto como mínimo. No se pueden pedir por aquí: se piden en la tienda con el DXF (como_pedirlo)."""
    return await precio_fabricado(clave_ctx(ctx), tipo, dict(texto=texto or None, alto_mm=alto_mm or None, piezas=piezas,
                                  opciones=opciones or {}, cantidad=cantidad))


@mcp.tool()
async def precio_neon(ctx: Context, ancho_mm: float, alto_mm: float, metros_neon: float = 0, piezas: int = 0, color: str = "rojo",
                      ambiente: str = "interior", fijacion: str = "atornillado", base: str = "metacrilato_10", cantidad: int = 1) -> dict:
    """Precio de un neón a medida (neón flexible de 6 mm en base fresada, con su fuente; cálculo por costes de la tienda).
    ancho_mm y alto_mm: medida del rótulo con la base. metros_neon y piezas: si no se saben, se estiman (precio orientativo).
    color: rojo | blanco | blanco_calido | violeta | rosa | naranja | amarillo | azul | turquesa | verde | rgb
    (en exterior solo blanco, blanco_calido, rojo, violeta, verde y rgb). ambiente: interior | exterior.
    fijacion: atornillado | colgado | autoportante. base: metacrilato_10 | metacrilato_6 | metacrilato_5 | pvc_19.
    El regulador con mando (o el controlador RGB, obligatorio con rgb) va aparte. Se pide en la tienda con el simulador (como_pedirlo)."""
    return await precio_fabricado(clave_ctx(ctx), "neon", dict(ancho_mm=ancho_mm, alto_mm=alto_mm, metros_neon=metros_neon or None,
                                  piezas=piezas or None, color=color, ambiente=ambiente, fijacion=fijacion, base=base, cantidad=cantidad))


@mcp.tool()
async def crear_pedido_pendiente(ctx: Context, lineas: list[dict], entrega: Optional[dict] = None) -> dict:
    """Crea un pedido PENDIENTE de confirmación. lineas: [{variante_id, cantidad}], [{producto_id, ancho_mm, alto_mm, seleccion, cantidad}]
    o la linea_para_pedido de precio_bandeja_ciega / precio_bandejas_esquina.
    entrega: {"modo": "recogida"} o {"modo": "envio", "direccion": "...", "observaciones": "..."}. Devuelve el total con IVA: enséñaselo al usuario antes de confirmar."""
    return await crear_pedido(clave_ctx(ctx), lineas, entrega, False)


@mcp.tool()
async def confirmar_pedido(ctx: Context, pedido_id: str) -> dict:
    """Confirma un pedido pendiente (solo después de que el usuario haya aprobado el total)."""
    return await rpc("api_confirmar_pedido", {"p_clave": clave_ctx(ctx), "p_pedido": pedido_id})


@mcp.tool()
async def anular_pedido(ctx: Context, pedido_id: str) -> dict:
    """Anula un pedido pendiente que aún no se ha confirmado."""
    return await rpc("api_cancelar_pedido", {"p_clave": clave_ctx(ctx), "p_pedido": pedido_id})


@mcp.tool()
async def mis_pedidos(ctx: Context, limite: int = 20) -> list:
    """Últimos pedidos del cliente con su estado, líneas e importes."""
    return await rpc("api_pedidos", {"p_clave": clave_ctx(ctx), "p_limite": limite})


_mcp_app = mcp.streamable_http_app()


class MCPConClaveEnRuta:
    """/mcp/<clave> → pasa la clave en scope.state y reenvía al servidor MCP (para clientes que no admiten cabeceras)."""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            m = re.match(r"^/mcp/(tv_[a-z]+_[0-9a-f]{48})/?$", path)
            scope = dict(scope)
            scope["state"] = dict(scope.get("state") or {})
            if m:
                scope["state"]["clave"] = m.group(1)
            scope["path"] = "/"
            scope["raw_path"] = b"/"
        return await self.inner(scope, receive, send)


app.mount("/mcp", MCPConClaveEnRuta(_mcp_app))

# conector interno (Claude Team): OAuth con el usuario del panel + MCP de solo lectura (interno.py)
from starlette.routing import Route as _Ruta, Mount as _Montaje
app.include_router(INT.router)
app.router.routes.append(_Ruta(INT.RUTA_MCP, endpoint=INT.app_mcp))
app.router.routes.append(_Montaje(INT.RUTA_MCP, app=INT.app_mcp))


@app.on_event("startup")
async def _arranque():
    # el session manager de MCP necesita arrancar dentro del bucle de eventos
    app.state._mcp_cm = mcp.session_manager.run()
    await app.state._mcp_cm.__aenter__()
    app.state._mcp_int_cm = INT.mcp_int.session_manager.run()
    await app.state._mcp_int_cm.__aenter__()


@app.on_event("shutdown")
async def _parada():
    try:
        await app.state._mcp_cm.__aexit__(None, None, None)
    except Exception:
        pass
    try:
        await app.state._mcp_int_cm.__aexit__(None, None, None)
    except Exception:
        pass
