# -*- coding: utf-8 -*-
"""Conector interno de Tecnovinyl para Claude (plan Team): servidor MCP de solo lectura con login del panel.

Alex 27/10/2026: «que todos los chicos de la oficina tengan Claude… que el comercial pregunte sobre las compras de
Impresiona y que tú le mandes un resumen».

- MCP en /interno/mcp (streamable HTTP, sin estado). Herramientas de solo lectura sobre las ventas de AHORA (las que se
  suben en Panel → Actualización), los pedidos de la web, la tarifa y los clientes (funciones interno_* del SQL 152).
- Acceso con OAuth 2.1 (lo que piden los conectores propios de Claude): el propietario añade el conector una vez y cada
  persona lo conecta con SU usuario del panel. Solo admin, oficina y jefe de taller; los puestos del taller no.
- Sin estado: códigos y fichas firmados (HS256) con una clave que sale de SUPABASE_SERVICE_KEY, así no hace falta otra
  variable en Railway. Cada petición vuelve a mirar que el usuario siga en usuarios_internos con un rol válido (5 min de
  caché): si se le quita el acceso en el panel, deja de entrar.
- Registro de clientes OAuth automático (RFC 7591) o con la identidad publicada de Claude (documento de metadatos en una
  URL https); solo se admiten direcciones de vuelta de Claude (claude.ai, claude.com, anthropic.com) o de este equipo
  (localhost).
- Cada consulta queda en interno_registro (quién, qué herramienta y con qué datos).
- Presupuestos de fabricados (27/10/2026, SQL 154): letras, neón y bandejas con el mismo cálculo que la tienda
  (interno_precio_fabricado). Desglose de costes solo para oficina y administración. Bandejas también en composite
  de color de la tarifa (como la amarilla RAL 1023 de Agromart).
"""
import os, re, json, time, base64, hmac, hashlib, secrets, html, asyncio
from typing import Any, Optional
from urllib.parse import urlencode, urlparse
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
import fabricados as FB
import bandejas as BJ

SB = os.environ.get("SUPABASE_URL", "https://vqfcfbqlycjgqmdfympr.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
# clave publicable (la misma que lleva la tienda en su código): solo sirve para el inicio de sesión con correo y contraseña
PUBLICABLE = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_UBb4-ax96_QXvWDqa1A-zg_d22VWWRu")
SECRETO = hashlib.sha256(("tecnovinyl-interno-v1:" + KEY).encode()).digest()
ROLES_OK = {"admin", "oficina", "jefe_taller"}
NOMBRE_ROL = {"admin": "administración", "oficina": "oficina", "jefe_taller": "jefe de taller"}
T_ACCESO, T_REFRESCO, T_CODIGO, T_PETICION = 3600, 60 * 86400, 300, 900
RUTA_MCP = "/interno/mcp"
HOSTS_VUELTA = ("claude.ai", "claude.com", "anthropic.com")

_db = httpx.AsyncClient(base_url=SB + "/rest/v1", timeout=40, headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
_web = httpx.AsyncClient(timeout=15)


# ----------------------------------------------------------------------------- firmas
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def firmar(datos: dict) -> str:
    cab = _b64(b'{"alg":"HS256","typ":"JWT"}')
    cuerpo = _b64(json.dumps(datos, separators=(",", ":"), ensure_ascii=False).encode())
    firma = _b64(hmac.new(SECRETO, f"{cab}.{cuerpo}".encode(), hashlib.sha256).digest())
    return f"{cab}.{cuerpo}.{firma}"


def leer(ficha: Optional[str], tipo: str) -> Optional[dict]:
    try:
        cab, cuerpo, firma = (ficha or "").split(".")
        bien = _b64(hmac.new(SECRETO, f"{cab}.{cuerpo}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(bien, firma):
            return None
        d = json.loads(_unb64(cuerpo))
    except Exception:
        return None
    if d.get("typ") != tipo or float(d.get("exp", 0)) < time.time():
        return None
    return d


def base_de(request: Request) -> str:
    """https://<host> con el que ha llegado la petición (Railway o api.tecnovinyl.com)."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    if not host.startswith(("localhost", "127.0.0.1")):
        proto = "https"
    return f"{proto}://{host}"


def vuelta_valida(uri: str) -> bool:
    try:
        u = urlparse(uri)
    except Exception:
        return False
    h = (u.hostname or "").lower()
    if u.scheme == "https" and any(h == d or h.endswith("." + d) for d in HOSTS_VUELTA):
        return True
    return u.scheme in ("http", "https") and h in ("localhost", "127.0.0.1")


# ----------------------------------------------------------------------------- usuarios del panel
_usuarios: dict = {}   # user_id → (caduca, fila o None)


async def usuario_interno(user_id: str, fresco: bool = False) -> Optional[dict]:
    hit = _usuarios.get(user_id)
    if hit and hit[0] > time.time() and not fresco:
        return hit[1]
    fila = None
    if re.match(r"^[0-9a-f-]{36}$", user_id or ""):
        r = await _db.get("/usuarios_internos", params={"select": "user_id,nombre,rol", "user_id": "eq." + user_id})
        if r.status_code == 200 and r.json():
            fila = r.json()[0]
            if fila.get("rol") not in ROLES_OK:
                fila = None
    _usuarios[user_id] = (time.time() + 300, fila)
    return fila


# ----------------------------------------------------------------------------- clientes OAuth
_cimd: dict = {}   # url → (caduca, metadatos)


async def cliente_oauth(client_id: str) -> Optional[dict]:
    """{'redirect_uris': [...], 'nombre': ..., 'secreto': bool} o None."""
    if not client_id:
        return None
    if client_id.startswith("https://"):   # identidad publicada (Client ID Metadata Document)
        h = (urlparse(client_id).hostname or "").lower()
        if not any(h == d or h.endswith("." + d) for d in HOSTS_VUELTA):
            return None
        hit = _cimd.get(client_id)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            r = await _web.get(client_id, headers={"Accept": "application/json"})
            m = r.json() if r.status_code == 200 else None
        except Exception:
            m = None
        if not m or m.get("client_id") not in (None, client_id):
            return None
        meta = {"redirect_uris": [u for u in (m.get("redirect_uris") or []) if vuelta_valida(u)],
                "nombre": m.get("client_name") or "Claude", "secreto": False}
        _cimd[client_id] = (time.time() + 3600, meta)
        return meta
    d = leer(client_id, "cliente")
    if not d:
        return None
    return {"redirect_uris": d.get("r") or [], "nombre": d.get("n") or "cliente", "secreto": bool(d.get("s"))}


def secreto_de(client_id: str) -> str:
    return _b64(hmac.new(SECRETO, ("secreto:" + client_id).encode(), hashlib.sha256).digest())


def credenciales_cliente(request: Request, form) -> tuple:
    cid, sec = form.get("client_id"), form.get("client_secret")
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        try:
            a, b = base64.b64decode(auth[6:]).decode().split(":", 1)
            from urllib.parse import unquote
            cid, sec = unquote(a), unquote(b)
        except Exception:
            pass
    return cid, sec


# ----------------------------------------------------------------------------- rutas OAuth
router = APIRouter(include_in_schema=False)


def _json(d: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(d, status_code=status, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})


def meta_recurso(request: Request) -> dict:
    b = base_de(request)
    return {"resource": b + RUTA_MCP, "authorization_servers": [b], "scopes_supported": ["interno"],
            "bearer_methods_supported": ["header"], "resource_name": "Tecnovinyl · conector interno",
            "resource_documentation": b + "/interno"}


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/interno/mcp")
async def w_recurso(request: Request):
    return _json(meta_recurso(request))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/interno/mcp")
@router.get("/.well-known/openid-configuration")
async def w_servidor(request: Request):
    b = base_de(request)
    return _json({"issuer": b, "authorization_endpoint": b + "/oauth/authorize", "token_endpoint": b + "/oauth/token",
                  "registration_endpoint": b + "/oauth/register", "scopes_supported": ["interno"],
                  "response_types_supported": ["code"], "response_modes_supported": ["query"],
                  "grant_types_supported": ["authorization_code", "refresh_token"],
                  "code_challenge_methods_supported": ["S256"],
                  "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                  "client_id_metadata_document_supported": True})


@router.post("/oauth/register")
async def registrar(request: Request):
    try:
        m = await request.json()
    except Exception:
        return _json({"error": "invalid_client_metadata", "error_description": "JSON no válido"}, 400)
    uris = m.get("redirect_uris") or []
    if not uris or not all(isinstance(u, str) and vuelta_valida(u) for u in uris):
        return _json({"error": "invalid_redirect_uri", "error_description": "Solo se admiten direcciones de Claude"}, 400)
    metodo = m.get("token_endpoint_auth_method") or "none"
    if metodo not in ("none", "client_secret_post", "client_secret_basic"):
        metodo = "none"
    ahora = int(time.time())
    cid = firmar({"typ": "cliente", "r": uris, "n": str(m.get("client_name") or "cliente")[:80], "s": metodo != "none",
                  "iat": ahora, "exp": ahora + 10 * 365 * 86400})
    res = {"client_id": cid, "client_id_issued_at": ahora, "redirect_uris": uris, "token_endpoint_auth_method": metodo,
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
           "client_name": m.get("client_name") or "cliente", "scope": "interno"}
    if metodo != "none":
        res["client_secret"] = secreto_de(cid)
        res["client_secret_expires_at"] = 0
    return _json(res, 201)


def _pagina(cuerpo: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Conectar Claude · Tecnovinyl</title>
<style>
:root{{--bg:#0a0a0a;--card:#0f0f0f;--bd:#262626;--tx:#ededed;--dim:#a1a1a1;--ac:#2563eb;--bad:#f87171}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif;
min-height:100vh;display:grid;place-items:center;padding:24px max(16px,env(safe-area-inset-left))}}
.card{{width:100%;max-width:390px;background:var(--card);border:1px solid var(--bd);border-radius:1rem;padding:28px}}
.logo{{font-weight:800;letter-spacing:.04em;margin-bottom:18px}} .logo b{{color:#e10600}}
h1{{font-size:19px;margin:0 0 6px;font-weight:650}} p{{color:var(--dim);margin:0 0 6px;font-size:14px}}
label{{display:block;font-size:13px;color:var(--dim);margin:16px 0 6px}}
input{{width:100%;background:#141414;border:1px solid var(--bd);border-radius:.75rem;color:var(--tx);padding:11px 12px;font:inherit}}
input:focus{{outline:none;border-color:var(--ac)}}
button{{margin-top:22px;width:100%;background:var(--ac);color:#fff;border:0;border-radius:.75rem;padding:12px;font:inherit;font-weight:600;cursor:pointer}}
.err{{color:var(--bad);font-size:14px;margin-top:14px}} .nota{{font-size:12.5px;color:var(--dim);margin-top:16px}}
</style></head><body>{cuerpo}</body></html>""", status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def _formulario(peticion: str, cliente: str, error: str = "", email: str = "") -> HTMLResponse:
    return _pagina(f"""<form class="card" method="post" action="/oauth/authorize" autocomplete="on">
<div class="logo"><b>TECNO</b>VINYL</div>
<h1>Conectar {html.escape(cliente)} con Tecnovinyl</h1>
<p>Entra con tu usuario del panel. Claude podrá consultar ventas, clientes, pedidos de la web y tarifa. Solo leer: no cambia nada.</p>
<input type="hidden" name="t" value="{html.escape(peticion)}">
<label for="e">Correo</label><input id="e" name="email" type="email" autocomplete="username" required autofocus value="{html.escape(email)}">
<label for="c">Contraseña</label><input id="c" name="password" type="password" autocomplete="current-password" required>
{f'<div class="err">{html.escape(error)}</div>' if error else ''}
<button type="submit">Entrar y conectar</button>
<div class="nota">Solo para el equipo de Tecnovinyl (administración, oficina y jefe de taller).</div>
</form>""", 200 if not error else 401)


def _aviso(titulo: str, texto: str, status: int = 400) -> HTMLResponse:
    return _pagina(f'<div class="card"><div class="logo"><b>TECNO</b>VINYL</div><h1>{html.escape(titulo)}</h1><p>{html.escape(texto)}</p></div>', status)


@router.get("/oauth/authorize")
async def autorizar_get(request: Request):
    q = request.query_params
    cid, ru = q.get("client_id") or "", q.get("redirect_uri") or ""
    cli = await cliente_oauth(cid)
    if not cli:
        return _aviso("No se puede conectar", "Este programa no está autorizado para conectarse a Tecnovinyl.")
    if not ru and len(cli["redirect_uris"]) == 1:
        ru = cli["redirect_uris"][0]
    if ru not in cli["redirect_uris"]:
        return _aviso("No se puede conectar", "La dirección de vuelta no es la registrada.")
    def error(e, d):
        return RedirectResponse(ru + ("&" if "?" in ru else "?") + urlencode({"error": e, "error_description": d, **({"state": q["state"]} if q.get("state") else {})}), 302)
    if (q.get("response_type") or "code") != "code":
        return error("unsupported_response_type", "solo code")
    if not q.get("code_challenge") or (q.get("code_challenge_method") or "S256") != "S256":
        return error("invalid_request", "hace falta PKCE con S256")
    peticion = firmar({"typ": "peticion", "cid": cid, "ru": ru, "st": q.get("state"), "cc": q.get("code_challenge"),
                       "res": q.get("resource"), "sc": q.get("scope") or "interno", "exp": int(time.time()) + T_PETICION})
    return _formulario(peticion, cli["nombre"] if cli["nombre"].lower().startswith("claude") else "Claude")


@router.post("/oauth/authorize")
async def autorizar_post(request: Request):
    form = await request.form()
    p = leer(form.get("t"), "peticion")
    if not p:
        return _aviso("La página ha caducado", "Vuelve a pulsar «Conectar» en Claude.")
    email = (form.get("email") or "").strip()
    try:
        r = await _web.post(SB + "/auth/v1/token?grant_type=password", headers={"apikey": PUBLICABLE},
                            json={"email": email, "password": form.get("password") or ""})
    except Exception:
        return _formulario(form.get("t"), "Claude", "No se ha podido comprobar ahora mismo. Vuelve a probar en un momento.", email)
    if r.status_code != 200:
        await asyncio.sleep(1.0)   # frena los intentos seguidos
        return _formulario(form.get("t"), "Claude", "Correo o contraseña incorrectos.", email)
    uid = ((r.json() or {}).get("user") or {}).get("id") or ""
    u = await usuario_interno(uid, fresco=True)
    if not u:
        return _formulario(form.get("t"), "Claude", "Tu usuario no tiene acceso al conector (solo administración, oficina y jefe de taller).", email)
    codigo = firmar({"typ": "codigo", "cid": p["cid"], "ru": p["ru"], "cc": p["cc"], "res": p.get("res"), "sc": p.get("sc"),
                     "sub": uid, "nom": u.get("nombre"), "rol": u.get("rol"), "jti": secrets.token_urlsafe(12),
                     "exp": int(time.time()) + T_CODIGO})
    vuelta = p["ru"] + ("&" if "?" in p["ru"] else "?") + urlencode({"code": codigo, **({"state": p["st"]} if p.get("st") else {})})
    await registrar_uso(uid, u.get("nombre"), "conectar", {"cliente": (await cliente_oauth(p["cid"]) or {}).get("nombre")})
    return RedirectResponse(vuelta, 302)


_usados: dict = {}   # jti de códigos ya canjeados → caducidad


def _fichas(sub: str, nom: str, rol: str, cid: str, aud: str) -> dict:
    ahora = int(time.time())
    acceso = firmar({"typ": "acceso", "sub": sub, "nom": nom, "rol": rol, "cid": cid, "aud": aud, "iat": ahora, "exp": ahora + T_ACCESO})
    refresco = firmar({"typ": "refresco", "sub": sub, "cid": cid, "aud": aud, "jti": secrets.token_urlsafe(9), "iat": ahora, "exp": ahora + T_REFRESCO})
    return {"access_token": acceso, "token_type": "Bearer", "expires_in": T_ACCESO, "refresh_token": refresco, "scope": "interno"}


@router.post("/oauth/token")
async def ficha(request: Request):
    form = await request.form()
    cid, sec = credenciales_cliente(request, form)
    cli = await cliente_oauth(cid or "")
    if not cli:
        return _json({"error": "invalid_client"}, 401)
    if cli.get("secreto") and not hmac.compare_digest(sec or "", secreto_de(cid)):
        return _json({"error": "invalid_client"}, 401)
    gt = form.get("grant_type")
    for k in [k for k, v in _usados.items() if v < time.time()]:
        _usados.pop(k, None)
    if gt == "authorization_code":
        d = leer(form.get("code"), "codigo")
        if not d or d.get("cid") != cid or d.get("jti") in _usados:
            return _json({"error": "invalid_grant", "error_description": "código no válido o caducado"}, 400)
        if form.get("redirect_uri") and form.get("redirect_uri") != d.get("ru"):
            return _json({"error": "invalid_grant", "error_description": "redirect_uri distinta"}, 400)
        ver = form.get("code_verifier") or ""
        if _b64(hashlib.sha256(ver.encode()).digest()) != d.get("cc"):
            return _json({"error": "invalid_grant", "error_description": "PKCE no válido"}, 400)
        _usados[d["jti"]] = time.time() + T_CODIGO
        u = await usuario_interno(d["sub"], fresco=True)
        if not u:
            return _json({"error": "invalid_grant", "error_description": "usuario sin acceso"}, 400)
        return _json(_fichas(d["sub"], u.get("nombre"), u.get("rol"), cid, d.get("res") or (base_de(request) + RUTA_MCP)))
    if gt == "refresh_token":
        d = leer(form.get("refresh_token"), "refresco")
        if not d or d.get("cid") != cid:
            return _json({"error": "invalid_grant", "error_description": "vuelve a conectar"}, 400)
        u = await usuario_interno(d["sub"], fresco=True)
        if not u:
            return _json({"error": "invalid_grant", "error_description": "usuario sin acceso"}, 400)
        return _json(_fichas(d["sub"], u.get("nombre"), u.get("rol"), cid, d.get("aud") or (base_de(request) + RUTA_MCP)))
    return _json({"error": "unsupported_grant_type"}, 400)


@router.get("/interno")
async def portada_interno(request: Request):
    b = base_de(request)
    return _pagina(f"""<div class="card"><div class="logo"><b>TECNO</b>VINYL</div><h1>Conector interno para Claude</h1>
<p>Dirección para añadirlo en Claude (Ajustes → Conectores → Añadir conector propio):</p>
<p style="color:var(--tx);word-break:break-all"><b>{html.escape(b + RUTA_MCP)}</b></p>
<p>Cada persona lo conecta con su usuario del panel. Solo lectura: ventas de AHORA, clientes, pedidos de la web y tarifa.</p></div>""")


# ----------------------------------------------------------------------------- registro de uso
async def registrar_uso(uid: Optional[str], nombre: Optional[str], herramienta: str, args: dict):
    try:
        await _db.post("/interno_registro", json={"usuario": uid, "nombre": nombre, "herramienta": herramienta,
                                                  "argumentos": args}, headers={"Prefer": "return=minimal"})
    except Exception:
        pass


# ----------------------------------------------------------------------------- MCP
mcp_int = FastMCP("Tecnovinyl · interno", instructions=(
    "Conector interno de Tecnovinyl (solo lectura) para el equipo de la oficina. Datos: las ventas de AHORA (albaranes; se "
    "cargan desde el panel, normalmente cada viernes: mira «datos_hasta» y dilo si la pregunta va más allá), los pedidos de "
    "la tienda web, la tarifa de AHORA y los clientes. Importes en euros SIN IVA salvo que ponga «con_iva». "
    "Contesta en castellano, con un resumen corto, cifras redondeadas y lo más importante primero (total, cuándo, qué). "
    "Si una herramienta devuelve «elige», pregunta cuál de esos clientes es. No inventes: si un dato no está, dilo. "
    "Para clientes usa su nombre o su código de AHORA. En AHORA el metacrilato aparece como «META». "
    "Son datos internos: no los mandes a clientes ni fuera de la empresa sin que lo pida quien pregunta. "
    "PRESUPUESTOS de fabricados (letras corpóreas, neón, bandejas): usa precio_letras, precio_neon, precio_bandeja_ciega y "
    "precio_bandejas_esquina, que calculan igual que la tienda. Antes, pregunta lo que falte (medidas, color o RAL, luz, "
    "fijación, trasera). Da el PVP de tarifa y el neto (profesionales −30 %) sin IVA, una línea por cosa. Con texto y alto, "
    "el precio de las letras es orientativo: dilo. Lo que venga en «solo_oficina» (costes, márgenes, tiempos) nunca va "
    "al cliente."),
    stateless_http=True, json_response=True, streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


def quien(ctx: Context) -> dict:
    req = ctx.request_context.request
    u = ((req.scope.get("state") or {}).get("usuario") if req is not None else None) or {}
    if not u.get("sub"):
        raise PermissionError("sin usuario")
    return u


async def consulta(ctx: Context, herramienta: str, fn: str, args: dict) -> Any:
    u = quien(ctx)
    asyncio.create_task(registrar_uso(u.get("sub"), u.get("nom"), herramienta, {k: v for k, v in args.items() if v is not None}))
    r = await _db.post("/rpc/" + fn, json=args, headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except Exception:
            msg = r.text
        return {"error": "No se ha podido consultar: " + str(msg)[:300]}
    return r.json()


def _f(v: Optional[str]) -> Optional[str]:
    """Fecha AAAA-MM-DD (también admite DD/MM/AAAA)."""
    if not v:
        return None
    v = str(v).strip()
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", v)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return v[:10]


@mcp_int.tool()
async def quien_soy(ctx: Context) -> dict:
    """Con qué usuario del panel estás conectado y hasta qué fecha hay ventas de AHORA cargadas."""
    u = quien(ctx)
    hasta = await consulta(ctx, "quien_soy", "interno_datos_hasta", {})
    return {"usuario": u.get("nom"), "puesto": NOMBRE_ROL.get(u.get("rol"), u.get("rol")), "ventas_de_ahora_hasta": hasta}


@mcp_int.tool()
async def buscar_clientes(ctx: Context, texto: str, limite: int = 10) -> Any:
    """Busca clientes por nombre (o parte), código de AHORA, NIF o correo. Devuelve contacto, si tiene cuenta en la web,
    lo comprado este año (neto sin IVA) y la última compra."""
    return await consulta(ctx, "buscar_clientes", "interno_buscar_clientes", {"p_texto": texto, "p_limite": limite})


@mcp_int.tool()
async def compras_cliente(ctx: Context, cliente: str, desde: Optional[str] = None, hasta: Optional[str] = None) -> Any:
    """Resumen de lo que ha comprado un cliente según los albaranes de AHORA: total neto sin IVA, albaranes, compras por
    mes, artículos más comprados, últimos albaranes, mismo periodo del año anterior (si está cargado) y pedidos web.
    «cliente»: nombre, parte del nombre o código de AHORA. Fechas AAAA-MM-DD; por defecto, desde el 1 de enero hasta hoy."""
    return await consulta(ctx, "compras_cliente", "interno_compras_cliente", {"p_cliente": cliente, "p_desde": _f(desde), "p_hasta": _f(hasta)})


@mcp_int.tool()
async def albaranes(ctx: Context, cliente: Optional[str] = None, numero_albaran: Optional[int] = None,
                    desde: Optional[str] = None, hasta: Optional[str] = None, limite: int = 10) -> Any:
    """Albaranes de AHORA con sus líneas (referencia, artículo, unidades, precio, dto. y neto): los últimos de un cliente o
    uno concreto por su número."""
    return await consulta(ctx, "albaranes", "interno_albaranes", {"p_cliente": cliente, "p_desde": _f(desde), "p_hasta": _f(hasta),
                                                                   "p_albaran": numero_albaran, "p_limite": limite})


@mcp_int.tool()
async def ventas_articulo(ctx: Context, texto: str, desde: Optional[str] = None, hasta: Optional[str] = None) -> Any:
    """Cuánto se ha vendido de un artículo (referencia de AHORA o palabras de la descripción): unidades, neto, por mes,
    referencias que encajan y qué clientes lo compran más. Por defecto, este año."""
    return await consulta(ctx, "ventas_articulo", "interno_ventas_articulo", {"p_texto": texto, "p_desde": _f(desde), "p_hasta": _f(hasta)})


@mcp_int.tool()
async def resumen_ventas(ctx: Context, desde: Optional[str] = None, hasta: Optional[str] = None) -> Any:
    """Ventas de toda la empresa en un periodo (albaranes de AHORA): total neto, por mes, mejores clientes, artículos más
    vendidos y pedidos de la web. Por defecto, este año."""
    return await consulta(ctx, "resumen_ventas", "interno_resumen_ventas", {"p_desde": _f(desde), "p_hasta": _f(hasta)})


@mcp_int.tool()
async def clientes_sin_comprar(ctx: Context, dias: int = 60, minimo_euros: float = 300) -> Any:
    """Clientes que han comprado en el último año (al menos «minimo_euros» netos) y llevan más de «dias» días sin comprar:
    para que el comercial los llame."""
    return await consulta(ctx, "clientes_sin_comprar", "interno_clientes_sin_compras", {"p_dias": dias, "p_minimo": minimo_euros})


@mcp_int.tool()
async def pedidos_web(ctx: Context, cliente: Optional[str] = None, estado: Optional[str] = None, limite: int = 20) -> Any:
    """Pedidos y presupuestos de la tienda web (los más nuevos primero), con estado, importe, nº de pedido y de albarán en
    AHORA y la revisión. Filtros opcionales: cliente y estado (p. ej. confirmado, produccion, terminado, cancelado,
    borrador) o tipo (pedido, presupuesto)."""
    return await consulta(ctx, "pedidos_web", "interno_pedidos_web", {"p_cliente": cliente, "p_estado": estado, "p_limite": limite})


@mcp_int.tool()
async def pedido_web(ctx: Context, codigo: str) -> Any:
    """Un pedido de la web con todas sus líneas, entrega, pago, estado en el taller y números de AHORA. «codigo»: el de la
    web (W26-…) o el nº de pedido de AHORA."""
    return await consulta(ctx, "pedido_web", "interno_pedido_web", {"p_codigo": codigo})


@mcp_int.tool()
async def buscar_articulos(ctx: Context, texto: str, limite: int = 15) -> Any:
    """Artículos de la tarifa de AHORA por referencia o palabras: PVP, Dto. 1, precio con el Dto. 1, unidad, si está en la
    tienda web y cuánto se ha vendido este año. En AHORA el metacrilato es «META» y muchos llevan la medida («3050X2050»)."""
    return await consulta(ctx, "buscar_articulos", "interno_buscar_articulos", {"p_texto": texto, "p_limite": limite})


@mcp_int.tool()
async def precio_cliente(ctx: Context, cliente: str, texto: str, cantidad: float = 1) -> Any:
    """Lo que le cuesta a un cliente concreto en la tienda web (con sus descuentos) un producto, buscado por referencia de
    AHORA o por palabras. Precio sin IVA, por unidad de venta."""
    return await consulta(ctx, "precio_cliente", "interno_precio_cliente", {"p_cliente": cliente, "p_texto": texto, "p_cantidad": cantidad})


# ----------------------------------------------------------------------------- presupuestos de fabricados (SQL 154)
# Alex 27/10/2026: «¿podrá también ayudar a mis chicos de la oficina a hacer presupuestos como hemos hecho con Agromart?».
# Mismo cálculo que la tienda (interno_precio_fabricado). El desglose de costes solo lo ven oficina y administración, como en
# la tienda; el resto (jefe de taller) solo el precio. Las bandejas pueden ir en composite de color de la tarifa
# (color_plancha: amarillo RAL 1023, rojo…), como la de Agromart Sa Cabaneta.
VEN_COSTES = {"admin", "oficina"}
PRECIO = {"precio_cent", "pvp_cent", "unitario_cent", "pvp_unitario_cent", "dto", "q", "error", "juntas"}
COSTES = {"desglose", "coste", "margen", "min_taller", "min_maquina", "pint", "plancha_ref", "pintura_m2", "fuente_txt", "min_fres",
          "fres", "taller", "con_margen", "neon_ref", "base_ref", "fuente_ref", "piezas_det", "piezas", "comp", "est", "m_tubo",
          "n_ref", "n_esc", "min_base", "mano_obra_extra", "plancha", "plancha_contra", "temporada_pct"}
TRASERAS = {"estructura": "con estructura de aluminio", "contra": "con contrabandeja", "sin": "sin trasera"}
TXT_OPC = {
    "luz": {"sin": "sin luz", "frio": "LED blanco frío", "calido": "LED blanco cálido", "rgb": "LED RGB"},
    "difusor": {"sin": "sin difusor", "opal": "difusor opal", "hielo": "difusor hielo"},
    "fijacion": {"varilla": "con varillas", "chupetes": "con chupetes", "sin": "sin fijación", "cinta": "con cinta adhesiva"},
    "modelo": {"retro_difusor": "retroiluminadas con difusor", "retro": "retroiluminadas", "zocalo": "con zócalo",
               "frontal": "frontiluminadas", "sinluz_varillas": "sin luz, con varillas", "sinluz_pared": "sin luz, directas a la pared"},
    "frontal": {"opal": "frente opal", "opal20": "frente opal de 20 mm", "hielo": "frente hielo"},
    "corte": {"fresadora": "corte con fresadora", "laser": "corte láser"},
}


def _eur(c) -> Optional[str]:
    if c is None:
        return None
    s = f"{c / 100:,.2f}"
    return s.replace(",", "·").replace(".", ",").replace("·", ".") + " €"


def _limpia(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if v in (None, "", [], {}, 0):
            continue
        out[k] = (f"{len(v)} piezas" if k == "piezas" and isinstance(v, list) else v)
    return out


async def _calcula(ctx: Context, herramienta: str, tipo: str, conf: dict, args: dict, planchas=None):
    """Devuelve (resultado, ve_costes) o (None, {error})."""
    u = quien(ctx)
    asyncio.create_task(registrar_uso(u.get("sub"), u.get("nom"), herramienta, _limpia(args)))
    ver = u.get("rol") in VEN_COSTES
    r = await _db.post("/rpc/interno_precio_fabricado", json={"p_tipo": tipo, "p": conf, "p_planchas": planchas, "p_desglose": ver},
                       headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except Exception:
            msg = r.text
        return None, {"error": "No se ha podido calcular: " + str(msg)[:300]}
    d = r.json()
    if d.get("error"):
        return None, {"error": d["error"] + (" Lleva juntas: hay que estudiarlo con el taller antes de dar precio." if d.get("juntas") else "")}
    return (d, ver), None


def _salida(concepto: str, d: dict, ver: bool, extra: Optional[dict] = None) -> dict:
    dto = float(d.get("dto") or 0)
    q = d.get("q") or 1
    neto, pvp = d.get("precio_cent"), d.get("pvp_cent")
    out = {"concepto": concepto, "cantidad": q, "pvp_tarifa": _eur(pvp), "neto": (_eur(neto) or "") + " + IVA",
           "descuento": f"{round(dto * 100)} %", "neto_con_iva": _eur(round(neto * 1.21)) if neto is not None else None,
           "neto_unidad": _eur(d.get("unitario_cent")) if q > 1 else None, "pvp_cent": pvp, "neto_cent": neto}
    detalle = {k: v for k, v in d.items() if k not in PRECIO and k not in COSTES and v is not None and k != "bandeja"}
    if detalle:
        out["detalle"] = detalle
    if extra:
        out.update(extra)
    if ver:
        costes = {k: d[k] for k in COSTES if k in d and d[k] is not None}
        if costes:
            out["solo_oficina"] = dict(costes, aviso="Costes, márgenes y tiempos internos: nunca se enseñan al cliente.")
        if d.get("temporada_pct"):
            out["temporada"] = f"Recargo de temporada activo: +{d['temporada_pct']} % (ya va en el precio)."
    return {k: v for k, v in out.items() if v is not None}


def _color_corto(desc: str, ref: str) -> str:
    s = (desc or "").upper()
    m = re.match(r"COMPOSITE 3 ?MM (.+?)(?:/|\s\d{4}\s?X)", s)
    nombre = (m.group(1) if m else s).strip().lower()
    ral = re.search(r"RAL (?:APROX\.? )?(\d{4})", s)
    return f"{nombre}{' RAL ' + ral.group(1) if ral else ''} ({ref})"


async def _plancha_color(texto: str):
    r = await _db.post("/rpc/interno_planchas_bandeja", json={"p_texto": texto}, headers={"Content-Type": "application/json"})
    lst = r.json() if r.status_code < 400 else []
    exacta = [x for x in lst if x["ref"].upper() == texto.strip().upper()]
    if len(exacta) == 1:
        return exacta[0], None
    if len(lst) == 1:
        return lst[0], None
    if not lst:
        return None, {"error": f"No encuentro una plancha de composite de 3 mm de color «{texto}» en la tarifa. Mira colores_composite, "
                               "o hazla en blanco pintada (acabado ral)."}
    return None, {"elige": [{"ref": x["ref"], "descripcion": x["descripcion"]} for x in lst],
                  "nota": "Hay varias planchas que encajan: pregunta cuál, o pasa la referencia en color_plancha."}


def _estima_lineas(lineas: list) -> tuple:
    """Piezas estimadas (como la API) para varias líneas de texto con su alto, y la caja del conjunto."""
    piezas, anchos, altos = [], [], []
    for i, ln in enumerate(lineas or []):
        t, h = str(ln.get("texto") or ""), ln.get("alto_mm")
        try:
            pz, _, caja = FB.piezas_de({"texto": t, "alto_mm": h})
        except FB.ErrorFab as e:
            raise FB.ErrorFab(f"lineas[{i}]: {e}")
        piezas += [{"ancho_mm": p["w"], "alto_mm": p["h"], "area_mm2": p["a"], "contorno_mm": p["p"]} for p in pz]
        anchos.append(caja["w"]); altos.append(caja["h"])
    if not piezas:
        raise FB.ErrorFab("lineas: hace falta al menos una línea con texto y alto_mm")
    alto = sum(altos) + round(0.3 * max(altos)) * (len(altos) - 1)
    return piezas, {"w": max(anchos), "h": alto}


def _resumen_letras(tipo: str, op: dict, d: dict) -> str:
    partes = [re.sub(r"\s*\(.*?\)", "", FB.TIPOS[tipo][1])]
    for k, v in op.items():
        if k in ("bandeja", "montaje", "ral") or v in (None, ""):
            continue
        if k == "acabado":
            partes.append(f"pintadas RAL {op.get('ral') or '(por indicar)'}" if v == "ral" else "sin pintar")
        elif k in TXT_OPC:
            partes.append(TXT_OPC[k].get(str(v), f"{k} {v}"))
        elif k in ("material", "color"):
            partes.append(str(v))
        elif k == "cara":
            partes.append(f"cara {v}")
        elif k == "grosor":
            partes.append(f"{v} mm")
        elif k == "fondo":
            partes.append(f"canto de {v} mm")
        else:
            partes.append(f"{k} {v}")
    if op.get("montaje") == "bandeja":
        partes.append("montadas sobre bandeja")
    if d.get("n"):
        partes.append(f"{d['n']} piezas" + (f", alto máx. {d['alto_max']} mm" if d.get("alto_max") else ""))
    return " · ".join(partes)


@mcp_int.tool()
async def opciones_fabricados(ctx: Context) -> dict:
    """Qué fabricados se pueden presupuestar con las herramientas precio_* y qué opciones admite cada uno. Mírala antes de
    calcular si dudas de un valor."""
    quien(ctx)
    return {
        "letras (precio_letras)": {t: {"producto": FB.TIPOS[t][1], "opciones": FB.OPCIONES[t]} for t in FB.OPCIONES},
        "neon (precio_neon)": {"ancho_mm/alto_mm": "medida del rótulo con la base", "metros_neon/piezas": "si no se saben, se estiman",
                               "color": "rojo | blanco | blanco_calido | violeta | rosa | naranja | amarillo | azul | turquesa | verde | rgb",
                               "ambiente": "interior | exterior (en exterior solo blanco, blanco_calido, rojo, violeta, verde y rgb)",
                               "fijacion": "atornillado | colgado | autoportante", "base": "metacrilato_10 | metacrilato_6 | metacrilato_5 | pvc_19"},
        "bandeja_ciega (precio_bandeja_ciega)": dict(BJ.PARAMS["bandeja_ciega"], color_plancha="composite de 3 mm de color de la tarifa "
                                                     "(amarillo RAL 1023, rojo, azul…; ver colores_composite) en vez de blanco o negro"),
        "bandejas_esquina (precio_bandejas_esquina)": BJ.PARAMS["bandejas_esquina"],
        "reglas": [
            "Es el mismo cálculo por costes que la tienda. A profesionales: PVP de tarifa −30 %. Precios sin IVA.",
            "Letras: con texto y alto el precio es ORIENTATIVO (se estima la forma); con las medidas de cada pieza del DXF "
            "(ancho, alto, superficie y contorno) es exacto. También se puede sacar exacto en la tienda (modo oficina) subiendo el DXF.",
            "Letras sin luz: 20 mm de alto como mínimo. Letras sobre bandeja: la bandeja tiene que sobresalir 50 mm por cada lado.",
            "Bandeja: composite de 3 mm, de 300 a 12.000 mm. Si abierta (lo corto + 2 × fondo) pasa de 1.480 mm lleva juntas: "
            "hay que estudiarla.",
            "Bandeja de color: blanco o negro, blanco pintado RAL (la pintura cuesta 150 € como mínimo) o una plancha de composite "
            "de color de la tarifa con color_plancha (sin pintar).",
            "Bandeja calada: necesita el DXF del cliente; se calcula en la tienda (modo oficina).",
        ]}


@mcp_int.tool()
async def colores_composite(ctx: Context, texto: str = "") -> Any:
    """Planchas de composite de 3 mm de color de la tarifa (además del blanco y el negro) que se pueden usar en las bandejas
    con color_plancha: referencia, descripción y ancho de la plancha. «texto» filtra (p. ej. «amarillo», «1023», «rojo»)."""
    u = quien(ctx)
    lst = await consulta(ctx, "colores_composite", "interno_planchas_bandeja", {"p_texto": texto or None})
    if not isinstance(lst, list):
        return lst
    if u.get("rol") not in VEN_COSTES:
        lst = [{k: v for k, v in x.items() if k != "precio_plancha"} for x in lst]
    return {"planchas": lst, "nota": "Además están el blanco y el negro (color en precio_bandeja_ciega) y el blanco pintado RAL."}


@mcp_int.tool()
async def precio_bandeja_ciega(ctx: Context, ancho_mm: float, alto_mm: float, fondo_mm: float, trasera: str, color: str = "",
                               color_plancha: str = "", acabado: str = "natural", ral: str = "", esquinas: str = "v",
                               cantidad: int = 1) -> Any:
    """Presupuesto de una bandeja ciega de composite de 3 mm a medida (mismo cálculo por costes que la tienda).
    trasera: estructura (de aluminio, lo normal en fachada) | contra (contrabandeja) | sin. Pregúntala si no la dicen.
    Color, una de tres: color = negro-mate | negro-brillo | blanco-mate | blanco-brillo; o acabado = "ral" con ral = "7016"
    (blanco pintado); o color_plancha = una plancha de composite de color de la tarifa (p. ej. "amarillo 1023" o su
    referencia; ver colores_composite). esquinas: v | recta. Devuelve PVP de tarifa, neto (−30 %) y, a la oficina, el desglose."""
    args = dict(ancho_mm=ancho_mm, alto_mm=alto_mm, fondo_mm=fondo_mm, trasera=trasera, color=color, color_plancha=color_plancha,
                acabado=acabado, ral=ral, esquinas=esquinas, cantidad=cantidad)
    d = dict(ancho_mm=ancho_mm, alto_mm=alto_mm, fondo_mm=fondo_mm, color=color, trasera=trasera, acabado=acabado, ral=ral,
             esquinas=esquinas, cantidad=cantidad)
    planchas, nombre = None, None
    if color_plancha.strip():
        pl, err = await _plancha_color(color_plancha)
        if err:
            return err
        planchas = [[pl["ref"], pl["ancho_mm"]]]
        d.update(color="blanco-mate", acabado="natural", ral="")
        nombre = _color_corto(pl["descripcion"], pl["ref"])
    elif not color and str(acabado).lower() == "ral":
        d["color"] = "blanco-mate"
    try:
        conf = BJ.conf_ciega(d)
    except BJ.ErrorBandeja as e:
        return {"error": str(e)}
    res, err = await _calcula(ctx, "precio_bandeja_ciega", "bandeja_ciega", conf, args, planchas)
    if err:
        return err
    r, ver = res
    color_txt = nombre or (f"blanco pintado RAL {conf['ral'] or '(por indicar)'}" if conf["acabado"] == "ral" else conf["color_nombre"].lower())
    concepto = (f"Bandeja ciega de composite {color_txt}, {conf['w']} × {conf['h']} × {conf['d']} mm, {TRASERAS[conf['trasera']]}"
                + (", esquinas rectas" if conf["esq"] == "recta" else ""))
    extra = {}
    if (r.get("n") or 1) > 1:
        extra["nota"] = f"Por su largo va en {r['n']} piezas de {r.get('lp')} × {r.get('c')} mm a tope sobre una sola estructura."
    return _salida(concepto, r, ver, extra)


@mcp_int.tool()
async def precio_bandejas_esquina(ctx: Context, tramos_mm: list[float], esquinas: list[dict], alto_mm: float, fondo_mm: float,
                                  color: str = "", medidas_de: str = "pared", acabado: str = "natural", ral: str = "") -> Any:
    """Presupuesto de un rótulo de bandejas de composite en una fachada con esquinas, sobre estructura de aluminio con inglete
    (mismo cálculo que la tienda). tramos_mm: largo de cada tramo, en orden. esquinas: una menos que tramos,
    [{"tipo": "saliente"|"entrante", "angulo": 90}] (ángulo entre las dos paredes). medidas_de: pared | rotulo.
    color: negro-mate | negro-brillo | blanco-mate | blanco-brillo, o acabado = "ral" con ral (blanco pintado).
    Devuelve el despiece (bandejas y pliegues), PVP de tarifa y neto (−30 %)."""
    args = dict(tramos_mm=tramos_mm, esquinas=esquinas, alto_mm=alto_mm, fondo_mm=fondo_mm, color=color, medidas_de=medidas_de,
                acabado=acabado, ral=ral)
    try:
        conf = BJ.conf_esquina(dict(args))
    except BJ.ErrorBandeja as e:
        return {"error": str(e)}
    res, err = await _calcula(ctx, "precio_bandejas_esquina", "bandejas_esquina", conf, args)
    if err:
        return err
    r, ver = res
    m = f"{conf['desarrollo'] / 1000:.2f}".replace(".", ",")
    concepto = (f"Bandejas de composite en esquina · {m} m de desarrollo × {conf['h']} mm, fondo {conf['d']} mm"
                f" · {len(conf['piezas'])} bandejas · {conf['color_nombre'].lower()} · con estructura de aluminio con inglete")
    return _salida(concepto, r, ver, {"despiece": BJ.despiece_legible(conf)})


@mcp_int.tool()
async def precio_letras(ctx: Context, tipo: str, texto: str = "", alto_mm: float = 0, lineas: Optional[list[dict]] = None,
                        piezas: Optional[list[dict]] = None, opciones: Optional[dict] = None, cantidad: int = 1,
                        ancho_conjunto_mm: float = 0, alto_conjunto_mm: float = 0, color_plancha_bandeja: str = "") -> Any:
    """Presupuesto de letras corpóreas o formas fabricadas por Tecnovinyl (mismo cálculo por costes que la tienda).
    tipo: letras_aluminio | letras_pvc_retro | letras_pvc_frontal | letras_metacrilato | letras_alupanel | letras_pvc.
    Medidas, una de tres: texto + alto_mm (una línea); lineas = [{"texto": "AGRO", "alto_mm": 170}, {"texto": "MART", ...}]
    para varias líneas o altos distintos (las dos dan precio ORIENTATIVO); o piezas = [{ancho_mm, alto_mm, area_mm2,
    contorno_mm}] de cada pieza del DXF (exacto). ancho_conjunto_mm/alto_conjunto_mm: medida total del rótulo si se sabe.
    opciones según el tipo (ver opciones_fabricados), p. ej. letras_pvc_retro {grosor: 19|30, material: blanco|negro,
    luz: sin|frio|calido|rgb, difusor: sin|opal|hielo, acabado: sin|ral, ral, fijacion: varilla|chupetes|sin,
    montaje: pared|bandeja, bandeja: {w, h, d, color: blanco-mate|negro-mate|…, acabado: natural|ral, ral,
    trasera: estructura|contra|sin}}. color_plancha_bandeja: bandeja en composite de color de la tarifa (p. ej. "amarillo 1023").
    Con montaje bandeja devuelve dos líneas: la bandeja y las letras con su montaje."""
    op = dict(opciones or {})
    args = dict(tipo=tipo, texto=texto, alto_mm=alto_mm, lineas=lineas, piezas=piezas, opciones=op, cantidad=cantidad,
                ancho_conjunto_mm=ancho_conjunto_mm, alto_conjunto_mm=alto_conjunto_mm, color_plancha_bandeja=color_plancha_bandeja)
    if tipo not in FB.OPCIONES:
        return {"error": "tipo no válido: " + ", ".join(FB.OPCIONES)}
    planchas, nombre = None, None
    if color_plancha_bandeja.strip():
        if op.get("montaje") != "bandeja" or not isinstance(op.get("bandeja"), dict):
            return {"error": "color_plancha_bandeja solo vale con opciones.montaje = bandeja y opciones.bandeja {w, h, d, trasera}."}
        pl, err = await _plancha_color(color_plancha_bandeja)
        if err:
            return err
        planchas = [[pl["ref"], pl["ancho_mm"]]]
        op["bandeja"] = dict(op["bandeja"], color="blanco-mate", acabado="natural", ral="")
        nombre = _color_corto(pl["descripcion"], pl["ref"])
    try:
        if lineas:
            pz, caja = _estima_lineas(lineas)
            conf, est = FB.conf_letras(tipo, {"piezas": pz, "opciones": op, "cantidad": cantidad})
            conf["caja"], est = caja, True
        else:
            conf, est = FB.conf_letras(tipo, {"texto": texto or None, "alto_mm": alto_mm or None, "piezas": piezas,
                                             "opciones": op, "cantidad": cantidad})
    except FB.ErrorFab as e:
        return {"error": str(e)}
    if ancho_conjunto_mm and alto_conjunto_mm:
        conf["caja"] = {"w": round(ancho_conjunto_mm), "h": round(alto_conjunto_mm)}
    res, err = await _calcula(ctx, "precio_letras", tipo, conf, args, planchas)
    if err:
        return err
    r, ver = res
    concepto = _resumen_letras(tipo, op, r)
    extra = {"estimado": est}
    if est:
        extra["nota"] = ("Precio ORIENTATIVO: se ha estimado la forma de las letras. Con el DXF (superficie y contorno de cada pieza) "
                         "sale el exacto.")
    b = r.get("bandeja")
    if isinstance(b, dict) and b.get("precio_cent"):
        dto = float(r.get("dto") or 0)
        bn = int(b["precio_cent"]); ln = int(r["precio_cent"]) - bn
        bp = round(bn / (1 - dto)) if dto < 1 else bn
        lp = int(r["pvp_cent"]) - bp
        bcolor = nombre or (f"blanco pintado RAL {b.get('ral') or '(por indicar)'}" if b.get("acabado") == "ral" else str(b.get("color") or ""))
        extra["lineas"] = [
            {"concepto": f"Bandeja ciega de composite {bcolor}, {b.get('w')} × {b.get('h')} × {b.get('d')} mm, "
                         f"{TRASERAS.get(b.get('trasera'), b.get('trasera'))}", "pvp_tarifa": _eur(bp), "neto": _eur(bn)},
            {"concepto": concepto + " (con el montaje sobre la bandeja)", "pvp_tarifa": _eur(lp), "neto": _eur(ln)}]
    return _salida(concepto, r, ver, extra)


@mcp_int.tool()
async def precio_neon(ctx: Context, ancho_mm: float, alto_mm: float, color: str = "rojo", metros_neon: float = 0, piezas: int = 0,
                      ambiente: str = "interior", fijacion: str = "atornillado", base: str = "metacrilato_10", cantidad: int = 1) -> Any:
    """Presupuesto de un neón a medida (neón flexible de 6 mm en base fresada, con su fuente; mismo cálculo que la tienda).
    ancho_mm y alto_mm: medida del rótulo con la base. metros_neon y piezas: si no se saben se estiman (precio orientativo;
    el simulador de la tienda da los exactos). color: rojo | blanco | blanco_calido | violeta | rosa | naranja | amarillo |
    azul | turquesa | verde | rgb (en exterior solo blanco, blanco_calido, rojo, violeta, verde y rgb). ambiente: interior |
    exterior. fijacion: atornillado | colgado | autoportante. base: metacrilato_10 | metacrilato_6 | metacrilato_5 | pvc_19.
    El regulador con mando (o el controlador RGB, obligatorio con rgb) va aparte."""
    args = dict(ancho_mm=ancho_mm, alto_mm=alto_mm, color=color, metros_neon=metros_neon, piezas=piezas, ambiente=ambiente,
                fijacion=fijacion, base=base, cantidad=cantidad)
    try:
        conf, est = FB.conf_neon(dict(args, metros_neon=metros_neon or None, piezas=piezas or None))
    except FB.ErrorFab as e:
        return {"error": str(e)}
    res, err = await _calcula(ctx, "precio_neon", "neon", conf, args)
    if err:
        return err
    r, ver = res
    concepto = (f"Neón a medida {conf['w']} × {conf['h']} mm, {conf['metros']} m de neón {color.replace('_', ' ')}, "
                f"{'exterior' if conf['ambiente'] == 'exterior' else 'interior'}, base {base.replace('_', ' ')}, {fijacion}")
    extra = {"estimado": est}
    if est:
        extra["nota"] = "Precio ORIENTATIVO: faltan los metros de neón y las piezas exactas (los da el simulador de la tienda)."
    return _salida(concepto, r, ver, extra)


_mcp_app = mcp_int.streamable_http_app()


class ProtegidoInterno:
    """Pide la ficha de acceso (Bearer) y deja el usuario en scope.state; si falta o no vale, 401 con la dirección de los
    metadatos (así Claude sabe dónde conectarse)."""
    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        proto = (hdrs.get("x-forwarded-proto") or scope.get("scheme") or "https").split(",")[0].strip()
        host = (hdrs.get("x-forwarded-host") or hdrs.get("host") or "").split(",")[0].strip()
        if not host.startswith(("localhost", "127.0.0.1")):
            proto = "https"
        base = f"{proto}://{host}"
        if scope.get("method") == "OPTIONS":
            return await _responde(send, 204, b"", {"access-control-allow-origin": "*", "access-control-allow-headers": "authorization, content-type, mcp-protocol-version, mcp-session-id",
                                                    "access-control-allow-methods": "GET, POST, DELETE, OPTIONS"})
        m = re.match(r"^\s*Bearer\s+(.+)$", hdrs.get("authorization") or "", re.I)
        d = leer(m.group(1).strip() if m else None, "acceso")
        u = await usuario_interno(d["sub"]) if d else None
        if not d or not u:
            cuerpo = json.dumps({"error": "invalid_token", "error_description": "Conéctate con tu usuario del panel"}).encode()
            return await _responde(send, 401, cuerpo, {
                "content-type": "application/json",
                "www-authenticate": f'Bearer realm="Tecnovinyl", error="invalid_token", resource_metadata="{base}/.well-known/oauth-protected-resource{RUTA_MCP}"',
                "access-control-allow-origin": "*", "access-control-expose-headers": "www-authenticate"})
        scope = dict(scope)
        scope["state"] = dict(scope.get("state") or {})
        scope["state"]["usuario"] = {"sub": d["sub"], "nom": u.get("nombre"), "rol": u.get("rol")}
        scope["path"], scope["raw_path"] = "/", b"/"
        return await self.inner(scope, receive, send)


async def _responde(send, status: int, cuerpo: bytes, cabeceras: dict):
    await send({"type": "http.response.start", "status": status,
                "headers": [(k.encode(), v.encode()) for k, v in cabeceras.items()] + [(b"content-length", str(len(cuerpo)).encode())]})
    await send({"type": "http.response.body", "body": cuerpo})


app_mcp = ProtegidoInterno(_mcp_app)
