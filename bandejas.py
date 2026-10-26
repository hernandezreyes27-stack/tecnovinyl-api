# -*- coding: utf-8 -*-
"""Bandejas de composite por API (23/10/2026). En la tienda son configuradores (no productos normales), así que aquí
se añaden al catálogo como «productos virtuales» y se traducen sus parámetros a lo que esperan las funciones del
servidor: api_precio_bandeja (99-api-bandejas.sql) y las líneas bandeja_ciega / bandeja_esquina de crear_pedido_para.
Precio = el mismo cálculo por costes que la tienda. La calada necesita el DXF del cliente: solo desde la tienda."""
from typing import Optional
import esquina

TIENDA = "https://www.tecnovinyl.com/tienda/"
COLORES = {"negro-mate": "Negro mate", "negro-brillo": "Negro brillo", "blanco-mate": "Blanco mate", "blanco-brillo": "Blanco brillo"}

VIRTUALES = [
    {"producto_id": "bandeja-ciega-composite-425", "slug": "bandeja-ciega-composite-425", "nombre": "Bandeja ciega composite",
     "tipo": "bandeja_ciega", "categoria": "Rótulos", "categoria_id": "rotulos-2", "bajo_pedido": False,
     "descripcion_corta": "Bandeja de composite de 3 mm plegada a medida (esquinas en V o rectas), sin trasera, con estructura de aluminio o con contrabandeja. Precio al momento.",
     "url": TIENDA + "#/bandejas/ciega"},
    {"producto_id": "bandejas-composite-en-esquina", "slug": "bandejas-composite-en-esquina", "nombre": "Bandejas composite en esquina",
     "tipo": "bandejas_esquina", "categoria": "Rótulos", "categoria_id": "rotulos-2", "bajo_pedido": False,
     "descripcion_corta": "Rótulo de bandejas de composite para fachadas con esquinas de cualquier ángulo o chaflán, sobre una estructura de aluminio con inglete. Se pasan los tramos y las esquinas; despiece y precio al momento.",
     "url": TIENDA + "#/bandejas/esquina"},
    {"producto_id": "bandeja-de-composite-calada-375", "slug": "bandeja-de-composite-calada-375", "nombre": "Bandeja de composite calada",
     "tipo": "dxf", "categoria": "Rótulos", "categoria_id": "rotulos-2", "bajo_pedido": False,
     "descripcion_corta": "Bandeja calada con el texto o logo del cliente, LED y relieve en metacrilato. Necesita el DXF: se pide en la tienda.",
     "url": TIENDA + "#/bandejas/calada"},
]

COMO = {
    "bandeja_ciega": "Configurador: precio con POST /v1/precio/bandeja-ciega (herramienta MCP precio_bandeja_ciega) y pedido con una línea "
                     "{\"bandeja_ciega\": {…los mismos parámetros…}, \"cantidad\": n}.",
    "bandejas_esquina": "Configurador: precio y despiece con POST /v1/precio/bandejas-esquina (herramienta MCP precio_bandejas_esquina) y pedido "
                        "con una línea {\"bandejas_esquina\": {…los mismos parámetros…}}.",
}

PARAMS = {
    "bandeja_ciega": {
        "ancho_mm": "ancho exterior (mín. 300)", "alto_mm": "alto exterior (mín. 300)", "fondo_mm": "fondo o canto, 20 a 300",
        "color": "negro-mate | negro-brillo | blanco-mate | blanco-brillo (composite de 3 mm; pregúntalo siempre al cliente)",
        "esquinas": "v (en V, por defecto) | recta",
        "trasera": "sin | estructura (estructura de aluminio 40×20) | contra (contrabandeja)",
        "acabado": "natural (color del composite) | ral (pintada; indica ral)", "ral": "código RAL si acabado = ral",
        "observaciones": "texto libre para el taller", "cantidad": "unidades iguales"},
    "bandejas_esquina": {
        "tramos_mm": "lista de largos de cada tramo de fachada, en orden, en mm",
        "esquinas": "lista (una menos que tramos) de {tipo: saliente|entrante, angulo: grados entre las dos paredes; 90 = esquina normal}",
        "alto_mm": "alto del rótulo", "fondo_mm": "fondo o canto, 20 a 300",
        "color": "negro-mate | negro-brillo | blanco-mate | blanco-brillo (pregúntalo siempre)",
        "medidas_de": "pared (medidas tomadas en la pared, por defecto) | rotulo (medida exterior de la cara del rótulo)",
        "acabado": "natural (color de la plancha) | ral (composite blanco pintado; indica ral)", "ral": "código RAL si acabado = ral",
        "observaciones": "texto libre (dirección de la obra, plazos…)"},
}


class ErrorBandeja(Exception):
    pass


def es_virtual(id_o_slug: str) -> Optional[dict]:
    return next((v for v in VIRTUALES if v["slug"] == id_o_slug or v["producto_id"] == id_o_slug), None)


def ficha_virtual(v: dict) -> dict:
    out = dict(v)
    out["como_se_pide"] = COMO.get(v["tipo"]) or "Se pide desde la tienda subiendo el DXF: " + v["url"]
    out["parametros"] = PARAMS.get(v["tipo"])
    out["descripcion"] = v["descripcion_corta"]
    out.update({"atributos": [], "combinaciones": [], "opciones": []})
    return out


def _num(d: dict, k: str, por_defecto=None) -> int:
    x = d.get(k, por_defecto)
    try:
        return int(round(float(x)))
    except Exception:
        raise ErrorBandeja(f"falta o no es válido: {k}")


def _color(d: dict) -> str:
    c = str(d.get("color") or "").strip().lower().replace(" ", "-")
    if c not in COLORES:
        raise ErrorBandeja("color: negro-mate, negro-brillo, blanco-mate o blanco-brillo (pregúntalo al cliente)")
    return c


def conf_ciega(d: dict) -> dict:
    d = dict(d or {})
    color = _color(d)
    esq = "recta" if str(d.get("esquinas") or d.get("esq") or "v").lower().startswith("rect") else "v"
    tras = str(d.get("trasera") or "sin").lower()
    if tras not in ("sin", "estructura", "contra"):
        raise ErrorBandeja("trasera: sin, estructura o contra")
    acab = "ral" if str(d.get("acabado") or "natural").lower() == "ral" else "natural"
    return {"w": _num(d, "ancho_mm"), "h": _num(d, "alto_mm"), "d": _num(d, "fondo_mm"), "esq": esq, "color": color,
            "color_nombre": COLORES[color], "acabado": acab, "ral": str(d.get("ral") or "").strip(), "trasera": tras,
            "observaciones": str(d.get("observaciones") or "").strip()[:1000],
            "q": max(1, _num(d, "cantidad", 1))}


def conf_esquina(d: dict) -> dict:
    d = dict(d or {})
    ral = str(d.get("acabado") or "natural").lower() == "ral"
    if ral:
        d["color"] = "blanco-mate"      # pintada RAL = composite blanco pintado (Alex 26/10/2026)
    color = _color(d)
    ralc = str(d.get("ral") or "").strip()[:30]
    tramos = d.get("tramos_mm") or d.get("tramos") or []
    try:
        tramos = [float(t.get("L") if isinstance(t, dict) else t) for t in tramos]
    except Exception:
        raise ErrorBandeja("tramos_mm: lista de números en mm")
    if not tramos or len(tramos) > 20 or any(t < 100 or t > 60000 for t in tramos):
        raise ErrorBandeja("tramos_mm: entre 1 y 20 tramos de 100 a 60.000 mm")
    esqs = []
    for e in d.get("esquinas") or []:
        tipo = "entrante" if str(e.get("tipo") or "").lower().startswith("entr") else "saliente"
        try:
            ang = float(e.get("angulo", e.get("ang")))
        except Exception:
            raise ErrorBandeja("cada esquina necesita angulo (grados)")
        if not 10 <= ang <= 180:
            raise ErrorBandeja("angulo de esquina entre 10 y 180 grados")
        esqs.append({"tipo": tipo, "ang": round(ang, 1)})
    if len(esqs) != len(tramos) - 1:
        raise ErrorBandeja("esquinas: tiene que haber una menos que tramos")
    H, D = _num(d, "alto_mm"), _num(d, "fondo_mm")
    if not 200 <= H <= 1500 or not 20 <= D <= 300:
        raise ErrorBandeja("alto_mm 200–1500 y fondo_mm 20–300")
    ref = "rotulo" if str(d.get("medidas_de") or "pared").lower().startswith("rot") else "pared"
    pl = 4050      # negro y blanco vienen en 4050 × 1500 (blanco 444752, Alex 26/10/2026)
    r = esquina.despiece(tramos, esqs, H, D, ref, pl)
    if r["avisos"] or not r["piezas"]:
        raise ErrorBandeja(" ".join(r["avisos"]) + " Pídelo como presupuesto en " + TIENDA + "#/bandejas/esquina")
    r5 = esquina._round
    return {"h": H, "d": D, "ref": ref, "color": color, "acabado": "ral" if ral else "natural", "ral": ralc if ral else "",
            "color_nombre": ("pintada RAL " + (ralc or "(por indicar)") + " sobre composite blanco") if ral else COLORES[color], "plancha": pl,
            "tramos": [{"L": r5(t)} for t in tramos], "esquinas": esqs, "desarrollo": r5(r["total"]),
            "piezas": [{"n": p["n"], "l": r5(p["L"]),
                        "pl": [{"x": r5(q["x"]), "dev": round(q["dev"], 2), "ang": round(q["ang"], 1)} for q in p["pl"]]}
                       for p in r["piezas"]],
            "observaciones": str(d.get("observaciones") or "").strip()[:1000], "q": 1}


def despiece_legible(conf: dict) -> list:
    return [{"bandeja": f"B{p['n']}", "largo_cara_mm": p["l"],
             "pliegues": [f"a {q['x']} mm, esquina de {q['ang']}°" for q in p["pl"]]} for p in conf["piezas"]]
