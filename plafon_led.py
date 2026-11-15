"""Iluminación de los plafones (tubos LED o módulos LED y su fuente).

Las mismas cuentas que hace la tienda, traducidas línea a línea de `web/src/simulador.js` (buildModulos, buildTubos…) y
de `web/src/plafon-led.js` (pledCalc, pledNota). Alex 15/11/2026: «quiero que la API esté perfecta»: en la tienda un
plafón «Con iluminación» lleva el +10 % de montaje en su precio y los tubos o módulos van en líneas aparte, con su
referencia y la tarifa del cliente; la API solo daba el plafón.

- Qué plafones: los que tienen `productos.limites.led = {"perfil", "tras", "con", "defecto"}` (Barcelona y Barcelona mini).
- Los parámetros (tubos, módulos, fuentes, separaciones) salen de `ajustes.simulador_led`, igual que en la tienda; si no
  está, valen los de aquí.
- Este módulo solo hace cuentas: no llama a la base de datos ni sabe de precios.
"""
import math
from typing import Optional

MOD4 = {"nombre": "LED 4 puntos REF: LED_40018 · SMD 2835 · 6000-6500 K · 223 lm/ud · lente 175° · IP67", "watts": 1.44,
        "sep": 15, "margenBorde": 7.5, "anchoMod": 4.26, "altoMod": 3.96, "maxPorCadena": 20, "puntos": 4, "ref": "LED_40018", "pack": 20}
MINI = {"nombre": "LED 2 puntos REF: LED_40013 · SMD 2835 · lente 175° · IP67 · 122 lm/ud", "watts": 0.72,
        "sep": 10, "margenBorde": 5, "anchoMod": 4.4, "altoMod": 1.7, "maxPorCadena": 20, "puntos": 2, "ref": "LED_40013", "pack": 20}
DEFECTO = {
    "perfiles": {
        "mini": {"nombre": "Barcelona Mini", "fondo": 9, "sistema": "modulos", "modulo": dict(MINI)},
        "bcn": {"nombre": "Barcelona", "fondo": 14, "sistema": "tubos", "sepFilasMax": 17, "modulo": dict(MOD4)},
        "bal": {"nombre": "Baleares", "fondo": 14, "sistema": "tubos", "sepFilasMax": 17, "modulo": dict(MOD4)},
        "tram": {"nombre": "Tramuntana", "fondo": 16, "sistema": "tubos", "sepFilasMax": 20, "modulo": dict(MOD4)},
    },
    "tubos": [{"largo": 60, "watts": 9, "ref": "764181021.1"}, {"largo": 90, "watts": 14, "ref": "764181041.1"},
              {"largo": 120, "watts": 18, "ref": "764181061.1"}, {"largo": 150, "watts": 22, "ref": "764181081.1"}],
    "tuboPreferido": 120,
    "solape": 10,
    "fuentes": [{"w": 12, "real": 12, "ref": "TR1212M"}, {"w": 18, "real": 18, "ref": "TR1812M"}, {"w": 20, "real": 20, "ref": "TR2012M"},
                {"w": 35, "real": 35, "ref": "TR3512M"}, {"w": 60, "real": 60, "ref": "TR6012M"}, {"w": 100, "real": 100, "ref": "TR10012M"},
                {"w": 150, "real": 120, "ref": "TR15012M"}, {"w": 240, "real": 192, "ref": "TR24012M"}, {"w": 320, "real": 264, "ref": "TR32012M"},
                {"w": 400, "real": 400, "ref": "ILU40012IP67"}, {"w": 480, "real": 480, "ref": "TR48012M"}],
    "margenFuente": 1.25,
}
REF_TIENDA = {"555181": "555175"}   # referencias del simulador que en la tienda se llaman de otra forma (SIM_REF_TIENDA)


def ref_tienda(r: str) -> str:
    return REF_TIENDA.get(r, r)


def _redondea(x: float) -> int:      # Math.round de JavaScript (para positivos)
    return int(math.floor(x + 0.5))


def _n(c: float) -> int:             # simN: de cm a mm
    return _redondea(c * 10)


def _num(x) -> str:                  # un número como lo escribe JavaScript: 14 y no 14.0
    return str(int(x)) if float(x) == int(x) else str(x)


def _pl(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def config(ajuste: Optional[dict]) -> dict:
    """Los parámetros del simulador: los de `ajustes.simulador_led` si están completos (simAplicaConfig), o los de aquí."""
    c = ajuste
    if not isinstance(c, dict) or not c.get("modulos") or not c.get("perfiles"):
        return DEFECTO
    mods = c["modulos"]
    mod4 = dict(MOD4, **mods["mod4"]) if mods.get("mod4") else dict(MOD4)
    perfiles = {}
    for k, p in c["perfiles"].items():
        m = mods.get(p.get("modulo")) or (mods.get("mini") if p.get("sistema") == "modulos" else mods.get("mod4")) or mod4
        perfiles[k] = dict(p, modulo=dict(m))
    out = {"perfiles": perfiles,
           "tubos": sorted(c["tubos"], key=lambda t: t["largo"]) if isinstance(c.get("tubos"), list) and c["tubos"] else DEFECTO["tubos"],
           "tuboPreferido": c.get("tuboPreferido") or DEFECTO["tuboPreferido"],
           "solape": c["solape"] if c.get("solape") is not None else DEFECTO["solape"],
           "fuentes": sorted(c["fuentes"], key=lambda f: f["real"]) if isinstance(c.get("fuentes"), list) and c["fuentes"] else DEFECTO["fuentes"],
           "margenFuente": c.get("margenFuente") or DEFECTO["margenFuente"]}
    return out


def referencias(cfg: dict) -> list:
    """Todas las referencias que puede pedir la iluminación de un plafón (para mirar cuáles están en la tienda)."""
    r = {t["ref"] for t in cfg["tubos"]} | {f["ref"] for f in cfg["fuentes"]} | {p["modulo"]["ref"] for p in cfg["perfiles"].values()}
    return sorted(ref_tienda(x) for x in r)


def build_modulos(ancho: float, alto: float, perfil: dict, cfg: dict) -> dict:
    mod = perfil["modulo"]; sep = mod["sep"]; margen = mod["margenBorde"]
    ancho_util, alto_util = max(ancho - 2 * margen, 1), max(alto - 2 * margen, 1)
    cols = max(1, _redondea(ancho_util / sep) + 1)
    filas = max(1, _redondea(alto_util / sep) + 1)
    total = cols * filas
    potencia = total * mod["watts"]
    pot_fuente = potencia * cfg["margenFuente"]
    fuentes = cfg["fuentes"]; mayor = fuentes[-1]
    if pot_fuente <= mayor["real"]:
        f = next(x for x in fuentes if x["real"] >= pot_fuente)
        fuente = {"ref": f["ref"], "n": 1, "txt": f"{_num(f['w'])} W IP67"}
    else:
        n = math.ceil(pot_fuente / mayor["real"])
        fuente = {"ref": mayor["ref"], "n": n, "txt": f"{n}×{_num(mayor['w'])} W IP67"}
    return {"sistema": "modulos", "mod": mod, "cols": cols, "filas": filas, "total": total, "potencia": potencia, "fuente": fuente}


def _llenar_fila(largo_fila: float, preferido: bool, cfg: dict) -> dict:
    tubos, solape, pref = cfg["tubos"], cfg["solape"], cfg["tuboPreferido"]
    fila, span = [], 0
    while True:
        primera = not fila
        rem = largo_fila - span
        caben = [t for t in tubos if (t["largo"] - (0 if primera else solape)) <= rem]
        if not caben:
            break
        t = next((x for x in caben if x["largo"] == pref), None) if preferido else None
        if t is None:
            t = caben[-1]
        fila.append(t); span += t["largo"] - (0 if primera else solape)
    return {"fila": fila, "span": span}


def build_tubos(ancho: float, alto: float, perfil: dict, cfg: dict) -> dict:
    horizontal, largo_fila, tramo_filas = True, ancho, alto
    minimo = cfg["tubos"][0]["largo"]
    if ancho < minimo and alto >= minimo:
        horizontal, largo_fila, tramo_filas = False, alto, ancho
    elif ancho < minimo and alto < minimo:
        return {"error": "el plafón es más pequeño que el tubo más corto"}
    a, b = _llenar_fila(largo_fila, True, cfg), _llenar_fila(largo_fila, False, cfg)
    cf = b if b["span"] > a["span"] else a          # a cobertura igual, gana la opción con el tubo preferido
    fila = cf["fila"]
    sep_max = perfil.get("sepFilasMax") or 17
    filas = 1 if tramo_filas <= sep_max * 1.2 else math.ceil((tramo_filas - sep_max) / sep_max) + 1
    return {"sistema": "tubos", "fila": fila, "filas": filas, "horizontal": horizontal, "totalTubos": len(fila) * filas,
            "potencia": sum(t["watts"] for t in fila) * filas}


def va_iluminado(led: Optional[dict], seleccion: Optional[dict]) -> bool:
    return bool(led) and (seleccion or {}).get(led.get("tras") or "Iluminación") == (led.get("con") or "Con iluminación")


def calcula(led: Optional[dict], seleccion: Optional[dict], ancho_mm, alto_mm, cantidad=1, tipo: Optional[str] = None,
            cfg: Optional[dict] = None, en_tienda: Optional[set] = None) -> Optional[dict]:
    """Lo que lleva un plafón iluminado (pledCalc): {tipo, caben, solo_modulos, piezas: [{ref, cant, txt, det, fuente?}], resumen}.
    None si no va iluminado o no se puede calcular. `tipo`: 'tubos' | 'modulos' (si no se dice, el de `led.defecto`).
    `en_tienda`: las referencias que hay en la tienda; si la fuente que toca no está, se coge entre las que sí."""
    if not va_iluminado(led, seleccion):
        return None
    cfg = cfg or DEFECTO
    perfil = cfg["perfiles"].get(led.get("perfil") or "bcn")
    if not perfil:
        return None
    a, h = float(ancho_mm or 0) / 10, float(alto_mm or 0) / 10      # el simulador calcula en cm
    if a < 10 or h < 10:
        return None
    tubos = cfg["tubos"]
    caben = perfil.get("sistema") == "tubos" and max(a, h) >= tubos[0]["largo"]
    pedido = tipo or ("modulos" if led.get("defecto") == "modulos" else "tubos")
    t = "tubos" if caben and pedido != "modulos" else "modulos"
    m = build_tubos(a, h, perfil, cfg) if t == "tubos" else build_modulos(a, h, perfil, cfg)
    if not m or m.get("error"):
        return None
    q = max(1, int(cantidad or 1))
    mas = f" × {q} plafones" if q > 1 else ""
    if t == "tubos":
        n: dict = {}
        for x in m["fila"]:
            n[x["largo"]] = n.get(x["largo"], 0) + 1
        piezas = []
        for largo in sorted(n, reverse=True):
            tb = next(x for x in tubos if x["largo"] == largo)
            piezas.append({"ref": tb["ref"], "cant": n[largo] * m["filas"] * q, "largo_mm": _n(largo), "txt": f"tubo LED de {_n(largo)} mm",
                           "det": f"tubo de {_n(largo)} mm · {_num(tb['watts'])} W · {n[largo]} por fila × {_pl(m['filas'], 'fila', 'filas')}{mas}"})
        resumen = " + ".join(f"{p['cant']} × {p['largo_mm']} mm" for p in piezas)
        desc = (f"{_pl(m['totalTubos'] * q, 'tubo LED', 'tubos LED')} ({len(m['fila'])} por fila × {_pl(m['filas'], 'fila', 'filas')}"
                f"{'' if m['horizontal'] else ', en vertical'}{mas}), luz blanca fría, a 230 V: sin fuente.")
    else:
        mod = m["mod"]; pack = mod.get("pack") or 20; tot = m["total"] * q
        fu = m["fuente"]
        if en_tienda is not None and ref_tienda(fu["ref"]) not in en_tienda:
            # la fuente que elige el simulador no está en la tienda: la más pequeña que llegue o varias de la mayor
            pot = m["potencia"] * cfg["margenFuente"]
            hay = sorted((f for f in cfg["fuentes"] if ref_tienda(f["ref"]) in en_tienda), key=lambda f: f["real"])
            if hay:
                f = next((x for x in hay if x["real"] >= pot), None); g = hay[-1]
                if f:
                    fu = {"ref": f["ref"], "n": 1, "txt": f"{_num(f['w'])} W IP67"}
                else:
                    k = math.ceil(pot / g["real"]); fu = {"ref": g["ref"], "n": k, "txt": f"{k}×{_num(g['w'])} W IP67"}
        piezas = [{"ref": mod["ref"], "cant": math.ceil(tot / pack), "txt": f"pack de {pack} módulos LED",
                   "det": f"{tot} módulos ({m['cols']} × {m['filas']}{mas}) en packs de {pack}"},
                  {"ref": fu["ref"], "cant": fu["n"] * q, "txt": "fuente de alimentación",
                   "det": f"fuente {fu['txt']}{(', ' + str(fu['n']) + ' por plafón') if q > 1 else ''}", "fuente": True}]
        una = fu["txt"].split("×", 1)[1] if fu["n"] > 1 else fu["txt"]
        resumen = (f"{tot} módulos ({_pl(piezas[0]['cant'], 'pack', 'packs')} de {pack}) y "
                   f"{(str(q * fu['n']) + ' fuentes') if q * fu['n'] > 1 else 'fuente'} de {una}")
        desc = f"{tot} módulos LED de {mod.get('puntos') or 4} puntos ({m['cols']} × {m['filas']}{mas}), luz blanca fría, con su fuente de alimentación."
    for p in piezas:
        p["ref"] = ref_tienda(p["ref"])
    return {"tipo": t, "caben_tubos": bool(caben), "solo_modulos": perfil.get("sistema") != "tubos", "cantidad": q,
            "piezas": piezas, "resumen": resumen, "descripcion": desc}


def nota(k: dict) -> str:
    """Lo que se apunta en la línea del plafón para el taller (pledNota)."""
    return (f"Iluminación con {'tubos LED' if k['tipo'] == 'tubos' else 'módulos LED'}: "
            + " + ".join(f"{p['cant']} × {p['txt']} (ref. {p['ref']})" for p in k["piezas"]) + ", en otras líneas del pedido")
