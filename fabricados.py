# -*- coding: utf-8 -*-
"""Letras corpóreas y neón por costes (Alex 26/10/2026: «mete en la API los precios»).

El precio lo calcula la base de datos con el mismo cálculo que la tienda (api_precio_fabricado, 121-api-fabricados.sql).
Aquí solo se traduce lo que manda el agente a lo que espera ese cálculo. Las letras se fabrican a partir de un DXF: si el
agente da las medidas de cada pieza (ancho, alto, superficie y contorno) el precio es el de la tienda; si solo da el texto y
el alto de letra, se estima la forma de cada letra y el precio es ORIENTATIVO. El neón igual: con los metros de neón que da
el simulador es exacto; si no, se estiman. El pedido de estos productos se hace en la tienda (DXF o simulador).
"""
from typing import Optional

TIENDA = "https://www.tecnovinyl.com/tienda/"


class ErrorFab(Exception):
    pass


TIPOS = {
    "letras_aluminio": ("letras-corporeas-de-aluminio-582", "Letras corpóreas de aluminio"),
    "letras_pvc_retro": ("letras-corporeas-pvc-retroiluminadas-581", "Letras corpóreas de PVC retroiluminadas (19 o 30 mm)"),
    "letras_pvc_frontal": ("letras-corporeas-pvc-iluminacion-frontal-707", "Letras corpóreas de PVC con iluminación frontal"),
    "letras_metacrilato": ("letras-y-formas-en-metacrilato-658", "Letras y formas en metacrilato (sin luz)"),
    "letras_alupanel": ("letras-y-formas-en-alupanel-608", "Letras y formas en alupanel 3 mm (sin luz)"),
    "letras_pvc": ("letras-y-formas-en-pvc-236", "Letras y formas en PVC (sin luz)"),
    "neon": ("neones-a-medida-234", "Neón a medida"),
}

# opciones que acepta cada tipo (se pasan tal cual al cálculo de la tienda)
OPCIONES = {
    "letras_aluminio": {"modelo": "retro_difusor | retro | zocalo | frontal | sinluz_varillas | sinluz_pared",
                        "fondo": "30–140 (mm de canto)", "luz": "frio | calido | rgb", "intensidad": "4.8 | 14.4",
                        "difusor": "3 (mm de opal)", "ral": "color RAL de la pintura"},
    "letras_pvc_retro": {"grosor": "19 | 30", "material": "blanco | negro (en 30 mm solo blanco)", "luz": "sin | frio | calido | rgb",
                         "difusor": "sin | opal | hielo", "acabado": "sin | ral", "ral": "RAL de las letras",
                         "fijacion": "varilla | chupetes | sin", "montaje": "pared | bandeja",
                         "bandeja": "{w, h, d, color: blanco-brillo|blanco-mate|negro-mate|negro-brillo, acabado: natural|ral, ral, trasera: estructura|contra|sin}"},
    "letras_pvc_frontal": {"luz": "frio | calido | rgb", "frontal": "opal | opal20 | hielo", "acabado": "sin | ral", "ral": "RAL del cuerpo",
                           "montaje": "pared | bandeja", "bandeja": "como en letras_pvc_retro"},
    "letras_metacrilato": {"material": "incoloro | opal | hielo", "grosor": "3 | 5 | 8 | 10", "corte": "fresadora | laser", "fijacion": "sin | cinta"},
    "letras_alupanel": {"color": "blanco | negro", "cara": "brillo | mate", "acabado": "sin | ral", "ral": "RAL", "fijacion": "sin | cinta"},
    "letras_pvc": {"color": "blanco | negro", "grosor": "3 | 5 | 10 | 19 | 30", "acabado": "sin | ral", "ral": "RAL", "fijacion": "sin | cinta"},
}

ESTRECHAS = set("iIl1!|.,:;'·")


def _num(v, nombre: str, minimo: float = 0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ErrorFab(f"{nombre}: hace falta un número")
    if x <= minimo:
        raise ErrorFab(f"{nombre}: tiene que ser mayor que {minimo:g}")
    return x


def piezas_de(d: dict):
    """Lista de piezas {a, p, w, h} (mm² y mm), si es estimado y la caja del conjunto {w, h}."""
    estimado = False
    out = []
    if d.get("piezas"):
        for i, pz in enumerate(d["piezas"]):
            w = _num(pz.get("ancho_mm"), f"piezas[{i}].ancho_mm")
            h = _num(pz.get("alto_mm"), f"piezas[{i}].alto_mm")
            a, p = pz.get("area_mm2"), pz.get("contorno_mm")
            if not a or not p:
                estimado = True
            out.append({"a": round(float(a) if a else 0.42 * w * h), "p": round(float(p) if p else 2.4 * (w + h)), "w": round(w), "h": round(h)})
        caja = {"w": round(sum(x["w"] for x in out) + 0.1 * max(x["h"] for x in out) * (len(out) - 1)), "h": max(x["h"] for x in out)}
    elif d.get("texto") and d.get("alto_mm"):
        h = _num(d["alto_mm"], "alto_mm")
        estimado = True
        n = 0
        ancho = 0.0
        for c in str(d["texto"]):
            if c.isspace():
                ancho += 0.35 * h
                continue
            w = (0.25 if c in ESTRECHAS else 0.62) * h
            out.append({"a": round(0.42 * w * h), "p": round(2.4 * (w + h)), "w": round(w), "h": round(h)})
            ancho += w + 0.1 * h
            n += 1
        if not n:
            raise ErrorFab("texto: no tiene letras")
        caja = {"w": round(ancho - 0.1 * h), "h": round(h)}
    else:
        raise ErrorFab("hace falta 'piezas' (ancho_mm, alto_mm, y si se tienen area_mm2 y contorno_mm de cada letra del DXF) "
                       "o 'texto' + 'alto_mm' (precio orientativo)")
    if len(out) > 400:
        raise ErrorFab("demasiadas piezas: pide presupuesto en la tienda")
    return out, estimado, caja


def conf_letras(tipo: str, d: dict):
    if tipo not in OPCIONES:
        raise ErrorFab("tipo no válido: " + ", ".join(t for t in TIPOS if t != "neon"))
    piezas, est, caja = piezas_de(d)
    op = dict(d.get("opciones") or {})
    permitidas = set(OPCIONES[tipo])
    raras = [k for k in op if k not in permitidas]
    if raras:
        raise ErrorFab(f"opciones no válidas para {tipo}: {', '.join(raras)}. Válidas: {', '.join(sorted(permitidas))}")
    conf = dict(op, piezas=piezas, caja=caja, q=max(1, int(d.get("cantidad") or 1)))
    if isinstance(conf.get("ral"), str):
        conf["ral"] = conf["ral"].strip()[:40]
    return conf, est


COLORES_NEON = {"rojo": "rojo", "blanco": "frio", "blanco_frio": "frio", "frio": "frio", "blanco_calido": "calido", "calido": "calido",
                "violeta": "morado", "morado": "morado", "rosa": "rosa", "naranja": "naranja", "amarillo": "amarillo", "azul": "azul",
                "turquesa": "hielo", "hielo": "hielo", "verde": "verde", "rgb": "rgb"}
BASES_NEON = {"metacrilato_10": "meta", "metacrilato_6": "meta6", "metacrilato_5": "meta5", "pvc_19": "pvc"}
FIJ_NEON = {"atornillado": "atornillado", "colgado": "colgante", "colgante": "colgante", "autoportante": "sin", "sin": "sin"}


def conf_neon(d: dict):
    w = _num(d.get("ancho_mm"), "ancho_mm")
    h = _num(d.get("alto_mm"), "alto_mm")
    est = False
    metros = d.get("metros_neon")
    if metros:
        metros = _num(metros, "metros_neon")
    else:   # unos 4,2 m de neón por metro de ancho en un rótulo de una línea (texto caligráfico de ejemplo)
        metros = round(4.2 * w / 1000, 2)
        est = True
    piezas = int(d.get("piezas") or 0) or max(2, round(metros / 0.42))
    if not d.get("piezas"):
        est = True
    color = COLORES_NEON.get(str(d.get("color") or "rojo").lower().strip().replace(" ", "_"))
    if not color:
        raise ErrorFab("color: " + " | ".join(sorted(set(COLORES_NEON) - {"frio", "calido", "morado", "hielo", "blanco_frio"})))
    base = BASES_NEON.get(str(d.get("base") or "metacrilato_10").lower().strip())
    if not base:
        raise ErrorFab("base: " + " | ".join(BASES_NEON))
    fij = FIJ_NEON.get(str(d.get("fijacion") or "atornillado").lower().strip())
    if not fij:
        raise ErrorFab("fijacion: atornillado | colgado | autoportante")
    amb = "exterior" if str(d.get("ambiente") or "").lower().strip() == "exterior" else "interior"
    conf = {"metros": metros, "piezas": piezas, "agujeros": 2 * piezas, "fijaciones": 4, "w": round(w), "h": round(h),
            "perimetro": None, "material": base, "rgb": color == "rgb", "color": color, "ambiente": amb, "fijacion": fij,
            "q": max(1, int(d.get("cantidad") or 1))}
    return conf, est


def conf(tipo: str, d: dict):
    return conf_neon(d) if tipo == "neon" else conf_letras(tipo, d)


def url_de(tipo: str) -> str:
    return TIENDA + "#/p/" + TIPOS[tipo][0]
