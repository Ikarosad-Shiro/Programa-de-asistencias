def detectar_sede_por_checador(usuarios_checad, trabajadores_mongo):
    conteo_por_sede = {}

    for usuario in usuarios_checad:
        coincidencias = [
            t for t in trabajadores_mongo
            if t.get("id_checador") == usuario.uid or t.get("nombre") == usuario.name
        ]

        for match in coincidencias:
            sede = match.get("sede")
            if sede:
                conteo_por_sede[sede] = conteo_por_sede.get(sede, 0) + 1

    if not conteo_por_sede:
        return None

    sede_mas_frecuente = max(conteo_por_sede, key=conteo_por_sede.get)
    total_coincidencias = sum(conteo_por_sede.values())
    porcentaje = round((conteo_por_sede[sede_mas_frecuente] / len(usuarios_checad)) * 100, 1) if usuarios_checad else 0

    return {
        "sede": sede_mas_frecuente,
        "coincidencias": conteo_por_sede[sede_mas_frecuente],
        "porcentaje": porcentaje
    }
