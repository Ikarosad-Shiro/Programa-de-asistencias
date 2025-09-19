# --- Parámetros del detector (ajustables) ---
from collections import defaultdict

W_PRINCIPAL = 1.0      # peso de coincidencias por sedePrincipal
W_FORANEA   = 0.35     # peso de coincidencias por sedesForaneas

CONF_FUERTE = 65.0     # % de confianza para propuesta fuerte
GAP_FUERTE  = 15.0     # diferencia vs 2° lugar para propuesta fuerte

CONF_DEBIL  = 50.0     # % de confianza para propuesta débil
GAP_DEBIL   = 10.0     # diferencia vs 2° lugar para propuesta débil

EVIDENCIA_MIN_IDS = 4  # IDs mapeados mínimos para decidir algo
ROLES_EXCLUIDOS = {"admin", "administrador", "dios", "superadmin"}

def _extraer_uid(u):
    """Obtiene el id de enrolamiento del usuario del checador (seguro en int)."""
    if isinstance(u, dict):
        for k in ("uid", "user_id", "uid_number", "enroll_id", "id", "uidNumber"):
            if k in u and u[k] is not None:
                try:
                    return int(str(u[k]).strip())
                except Exception:
                    pass
        return None
    for a in ("uid", "user_id", "uid_number", "enroll_id", "id", "uidNumber"):
        if hasattr(u, a):
            try:
                return int(str(getattr(u, a)).strip())
            except Exception:
                pass
    return None

def _trabajador_valido(t: dict) -> bool:
    """Activos, no-admin y con id_checador válido."""
    if str(t.get("estado", "")).lower() != "activo":
        return False
    rol = str(t.get("rol", "")).lower()
    if rol in ROLES_EXCLUIDOS:
        return False
    return t.get("id_checador") is not None

def detectar_sede_por_checador(usuarios_checad, trabajadores_mongo, params: dict | None = None):
    """
    Devuelve un dict con, al menos:
      - 'sede'         (int)   : sede más probable
      - 'porcentaje'   (float) : confianza 0-100 normalizada por IDs conocidos
      - 'coincidencias'(int)   : matches (principal+foráneos) de la sede top

    Y además meta-datos útiles:
      - 'decision'  : 'auto' | 'debil' | 'manual'
      - 'gap'       : diferencia vs 2do en puntos de confianza (0-100)
      - 'known_ids' : IDs del checador mapeados a trabajadores válidos
      - 'total_ids' : IDs totales leídos del checador
      - 'top3'      : breakdown de las 3 mejores sedes
    """
    # Lee parámetros opcionales
    wP = (params or {}).get("wP", W_PRINCIPAL)
    wF = (params or {}).get("wF", W_FORANEA)
    conf_fuerte = (params or {}).get("conf_fuerte", CONF_FUERTE)
    gap_fuerte  = (params or {}).get("gap_fuerte",  GAP_FUERTE)
    conf_debil  = (params or {}).get("conf_debil",  CONF_DEBIL)
    gap_debil   = (params or {}).get("gap_debil",   GAP_DEBIL)
    evidencia_min = (params or {}).get("evidencia_min", EVIDENCIA_MIN_IDS)

    # 1) IDs del checador
    ids_checador = []
    for u in usuarios_checad or []:
        n = _extraer_uid(u)
        if n is not None:
            ids_checador.append(n)
    if not ids_checador:
        return None

    # 2) Filtrar trabajadores válidos y mapear por id_checador
    validos = [t for t in (trabajadores_mongo or []) if _trabajador_valido(t)]
    if not validos:
        return None

    por_id = {}
    for t in validos:
        try:
            por_id[int(str(t.get("id_checador")))] = t
        except Exception:
            continue

    principal_matches = defaultdict(int)
    foranea_matches   = defaultdict(int)

    known_ids = 0
    for uid in ids_checador:
        t = por_id.get(uid)
        if not t:
            continue
        known_ids += 1

        sedeP = t.get("sedePrincipal", t.get("sede"))
        if isinstance(sedeP, int):
            principal_matches[sedeP] += 1

        for s in (t.get("sedesForaneas") or []):
            try:
                s_int = int(s)
                foranea_matches[s_int] += 1
            except Exception:
                continue

    sedes_candidatas = set(principal_matches) | set(foranea_matches)
    scores = {}
    for s in sedes_candidatas:
        p = principal_matches.get(s, 0)
        f = foranea_matches.get(s, 0)
        scores[s] = wP * p + wF * f

    total_ids = len(ids_checador)
    if known_ids < evidencia_min or not scores:
        return {
            "decision": "manual",
            "sede": None,
            "porcentaje": 0.0,
            "gap": 0.0,
            "known_ids": known_ids,
            "total_ids": total_ids,
            "top3": []
        }

    orden = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_sede, top_score = orden[0]
    second_score = orden[1][1] if len(orden) > 1 else 0.0

    to_pct = lambda sc: round(100.0 * (sc / max(1, known_ids)), 1)
    top_conf = to_pct(top_score)
    second_conf = to_pct(second_score)
    gap = round(top_conf - second_conf, 1)

    # top3 para mostrar en UI si quieres
    top3 = []
    for s, sc in orden[:3]:
        top3.append({
            "sede": int(s),
            "principal": int(principal_matches.get(s, 0)),
            "foraneos": int(foranea_matches.get(s, 0)),
            "score": round(sc, 2),
            "conf": to_pct(sc),
        })

    # Decisión
    if top_conf >= conf_fuerte and gap >= gap_fuerte:
        decision = "auto"
    elif top_conf >= conf_debil or gap >= gap_debil:
        decision = "debil"
    else:
        decision = "manual"

    return {
        "decision": decision,
        "sede": int(top_sede),
        "coincidencias": int(principal_matches.get(top_sede, 0) + foranea_matches.get(top_sede, 0)),
        "porcentaje": float(top_conf),  # mantiene la clave que ya usabas
        "gap": float(gap),
        "known_ids": int(known_ids),
        "total_ids": int(total_ids),
        "top3": top3
    }
