# -*- coding: utf-8 -*-
"""Despiece de las bandejas composite en esquina, igual que el configurador de la tienda
(maqueta/fachada-esquinas.html → calcula/divide/divideEsquina/divideIgual). Traducción línea a línea del JS:
si cambia el configurador hay que cambiar esto. Probado el 23/10/2026 con 400 fachadas al azar: mismo despiece que el JS.
Medidas en mm. El primer tramo va hacia +x y la calle queda a +y."""
import math

D2R = math.pi / 180
PLANCHA_ANCHO, MARGEN, PLIEGUE_MIN = 1500, 10, 30


def _round(x):  # Math.round de JS
    return math.floor(x + 0.5)


def poligonal(tramos, esquinas):
    P, dirs, a = [(0.0, 0.0)], [], 0.0
    for i, t in enumerate(tramos):
        dirs.append(a)
        p = P[-1]
        P.append((p[0] + t * math.cos(a), p[1] + t * math.sin(a)))
        if i < len(esquinas):
            e = esquinas[i]
            d = (180 - e["ang"]) * D2R
            a += -d if e["tipo"] == "saliente" else d
    return P, dirs


def _nor(a):
    return (-math.sin(a), math.cos(a))


def _inter(p, a, q, b):
    d1, d2 = (math.cos(a), math.sin(a)), (math.cos(b), math.sin(b))
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    t = ((q[0] - p[0]) * d2[1] - (q[1] - p[1]) * d2[0]) / den
    return (p[0] + t * d1[0], p[1] + t * d1[1])


def desplaza(P, dirs, k):
    n0 = _nor(dirs[0])
    out = [(P[0][0] + k * n0[0], P[0][1] + k * n0[1])]
    for i in range(1, len(P) - 1):
        na, nb = _nor(dirs[i - 1]), _nor(dirs[i])
        pa = (P[i][0] + k * na[0], P[i][1] + k * na[1])
        pb = (P[i][0] + k * nb[0], P[i][1] + k * nb[1])
        out.append(_inter(pa, dirs[i - 1], pb, dirs[i]) or pa)
    nl, L = _nor(dirs[-1]), P[-1]
    out.append((L[0] + k * nl[0], L[1] + k * nl[1]))
    return out


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def divide(a, b, F, Lmax, pata):
    F = sorted(F)
    r = divide_esquina(a, b, F, Lmax, pata)
    if r is not None:
        return r
    return divide_igual(a, b, F, Lmax, pata)


def divide_esquina(a, b, F, Lmax, pata):
    PUNTA_MIN, MIN = max(pata, 500), pata

    def ok(c):
        pts = [a] + c + [b]
        for i in range(len(pts) - 1):
            l = pts[i + 1] - pts[i]
            if l > Lmax + 0.5 or l < min(MIN, b - a) - 0.5:
                return False
        return all(all(abs(x - f) >= pata - 0.5 for f in F) for x in c)

    def relleno(desde, hasta):
        c, sg, x = [], (1 if hasta > desde else -1 if hasta < desde else 0), desde
        while abs(hasta - x) > Lmax:
            x += sg * Lmax
            c.append(x)
        resto = abs(hasta - x)
        if c and 0 < resto < PUNTA_MIN:
            ant = c[-2] if len(c) > 1 else desde
            c[-1] = (ant + hasta) / 2
        return c

    if not F:
        return [] if b - a <= Lmax else relleno(a, b)
    G = []
    for f in F:
        if G and f - G[-1][0] + 2 * pata <= Lmax:
            G[-1][1] = f
        else:
            G.append([f, f])
    if any(Lmax - (g[1] - g[0]) < 2 * pata for g in G):
        return None
    if F[0] - a < pata or b - F[-1] < pata:
        return None
    A = []
    for f0, f1 in G:
        pata2 = (Lmax - (f1 - f0)) / 2
        s0, s1 = f0 - pata2, f1 + pata2
        if s0 < a:
            s1 = min(b, s1 + (a - s0)); s0 = a
        if s1 > b:
            s0 = max(a, s0 - (s1 - b)); s1 = b
        A.append({"f0": f0, "f1": f1, "s0": s0, "s1": s1})
    for k in range(len(A) - 1):
        u, v = A[k], A[k + 1]
        if u["s1"] > v["s0"]:
            m = (u["f1"] + v["f0"]) / 2
            m = max(u["f1"] + pata, min(v["f0"] - pata, m))
            if m < u["f1"] + pata:
                return None
            u["s1"] = v["s0"] = m

    def ajusta_punta(lado):
        u = A[0] if lado < 0 else A[-1]
        hueco = u["s0"] - a if lado < 0 else b - u["s1"]
        if hueco <= 0 or hueco >= PUNTA_MIN:
            return True
        if u["s1"] - u["s0"] + hueco <= Lmax:
            if lado < 0:
                u["s0"] = a
            else:
                u["s1"] = b
            return True
        if lado < 0 and u["s1"] - hueco >= u["f1"] + pata:
            u["s0"] = a; u["s1"] -= hueco; return True
        if lado > 0 and u["s0"] + hueco <= u["f0"] - pata:
            u["s1"] = b; u["s0"] += hueco; return True
        if lado < 0:
            u["s0"] = a + PUNTA_MIN
            return u["s0"] <= u["f0"] - pata
        u["s1"] = b - PUNTA_MIN
        return u["s1"] >= u["f1"] + pata

    if not ajusta_punta(-1) or not ajusta_punta(1):
        return None
    c = []
    if A[0]["s0"] > a:
        c += list(reversed(relleno(A[0]["s0"], a))) + [A[0]["s0"]]
    for k in range(len(A)):
        u = A[k]
        v = A[k + 1] if k + 1 < len(A) else None
        if v is None:
            if u["s1"] < b:
                c += [u["s1"]] + relleno(u["s1"], b)
            break
        if v["s0"] > u["s1"]:
            gap = v["s0"] - u["s1"]
            if gap < MIN:
                m = (u["s1"] + v["s0"]) / 2
                u["s1"] = v["s0"] = m
            else:
                n = math.ceil(gap / Lmax)
                c.append(u["s1"])
                for j in range(1, n):
                    c.append(u["s1"] + gap * j / n)
                c.append(v["s0"])
                continue
        c.append(u["s1"])
    out = sorted(x for x in set(_round(x) for x in c) if a < x < b)
    return out if ok(out) else None


def divide_igual(a, b, F, Lmax, pata):
    S0 = b - a
    if S0 <= Lmax:
        return []
    n0 = math.ceil(S0 / Lmax)
    for n in range(n0, n0 + 7):
        c, okk, prev = [], True, a
        for k in range(1, n):
            x = a + k * S0 / n
            for f in F:
                if abs(x - f) < pata:
                    izq, der = f - pata, f + pata
                    x = izq if (x - izq < der - x and izq - prev > pata) else der
            if x - prev > Lmax or x - prev < pata:
                okk = False
                break
            c.append(x); prev = x
        if okk and b - prev <= Lmax and b - prev >= pata:
            return c
    return None


def despiece(tramos, esquinas, H, D, ref="pared", pl=4050, pata=300):
    """tramos: [mm]; esquinas: [{'tipo': 'saliente'|'entrante', 'ang': grados}] (una menos que tramos)."""
    P, dirs = poligonal(tramos, esquinas)
    if ref == "pared":
        cara = desplaza(P, dirs, D)
    else:
        cara = P
    Lc = [_dist(cara[i], cara[i + 1]) for i in range(len(cara) - 1)]
    acum = [0.0]
    for l in Lc:
        acum.append(acum[-1] + l)
    total = acum[-1]
    esq = []
    for i, e in enumerate(esquinas):
        pliega = e["tipo"] == "saliente" and PLIEGUE_MIN <= e["ang"] < 180
        esq.append({"i": i, "s": acum[i + 1], "tipo": e["tipo"], "ang": e["ang"], "dev": 180 - e["ang"],
                    "pliega": pliega, "recta": e["ang"] >= 179.5})
    Lmax = pl - 2 * MARGEN - 2 * D
    fuerza = [0.0] + [e["s"] for e in esq if not e["pliega"] and not e["recta"]] + [total]
    pliegues = [e for e in esq if e["pliega"] and not e["recta"]]
    piezas, av = [], []
    for k in range(len(fuerza) - 1):
        a, b = fuerza[k], fuerza[k + 1]
        F = [e for e in pliegues if a + 1 < e["s"] < b - 1]
        cortes = divide(a, b, [e["s"] for e in F], Lmax, pata)
        if cortes is None:
            av.append(f"No se puede repartir el tramo {a/1000:.2f}–{b/1000:.2f} m respetando la pata de {pata} mm.")
            continue
        pts = [a] + list(cortes) + [b]
        for j in range(len(pts) - 1):
            s0, s1 = pts[j], pts[j + 1]
            piezas.append({"s0": s0, "s1": s1, "L": s1 - s0,
                           "pl": [{"x": e["s"] - s0, "dev": e["dev"], "ang": e["ang"]} for e in F if s0 < e["s"] < s1]})
    for i, p in enumerate(piezas):
        p["n"] = i + 1
    if H + 2 * D > PLANCHA_ANCHO - 2 * MARGEN:
        av.append(f"Con {H} mm de alto y {D} mm de fondo la bandeja abierta mide {H + 2*D} mm y no cabe en la plancha de 1500: lleva juntas, hay que estudiarla con Tecnovinyl.")
    return {"total": total, "caras": Lc, "piezas": piezas, "avisos": av}
