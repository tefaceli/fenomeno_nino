# Arma la ficha de Tu zona: un solo JSON con lo que muestra la ficha de cada parroquia, a partir de los
# productos de cada fuente en <fuente>/data/out/. Corre al final de los workflows de INAMHI y Alístate.
# Si falta un insumo o no cuadra, no se escribe nada y el script sale con código 1.

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

# ---------- Configuración ----------

CARPETA_FUENTE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA_RAIZ = os.path.dirname(CARPETA_FUENTE)
INAMHI = os.path.join(CARPETA_RAIZ, "inamhi", "data", "out", "inamhi_parroquias.json")
INUNDACIONES = os.path.join(CARPETA_RAIZ, "inundaciones", "data", "out", "inundaciones_parroquias.json")
MOVIMIENTOS_MASA = os.path.join(CARPETA_RAIZ, "mm", "data", "out", "movimientos_masa_parroquias.json")
ALISTATE = os.path.join(CARPETA_RAIZ, "alistate", "data", "out", "alistate_eventos.geojson")
MAREAS = os.path.join(CARPETA_RAIZ, "marea", "data", "out", "mareas.json")
SALIDA = os.path.join(CARPETA_FUENTE, "data", "out", "ficha_parroquias.json")

TOTAL_PARROQUIAS = 1240
LLAVE = ["codpro", "codpar"]

ECUADOR = timezone(timedelta(hours=-5))
ahora = datetime.now(ECUADOR)
print("Corrida:", ahora.isoformat(timespec="seconds"))

# ---------- 1. Insumos ----------

insumos = {}
try:
    for nombre, ruta in [("inamhi", INAMHI), ("inundaciones", INUNDACIONES), ("movimientos_masa", MOVIMIENTOS_MASA),
                         ("alistate", ALISTATE), ("mareas", MAREAS)]:
        with open(ruta, encoding="utf-8") as f:
            insumos[nombre] = json.load(f)
except (OSError, ValueError) as error:
    print("ERROR al leer un insumo:", error, file=sys.stderr)
    sys.exit(1)

try:
    inamhi = pd.DataFrame(insumos["inamhi"]["parroquias"])
    inundaciones = pd.DataFrame(insumos["inundaciones"]["parroquias"])
    movimientos_masa = pd.DataFrame(insumos["movimientos_masa"]["parroquias"])
    mareas_parroquias = pd.DataFrame(insumos["mareas"]["parroquias"])
    eventos = pd.DataFrame([f["properties"] for f in insumos["alistate"]["features"]])
    pleamares = pd.DataFrame(insumos["mareas"]["pleamares"])
    estaciones = pd.DataFrame(insumos["mareas"]["estaciones"])
except KeyError as error:
    print("ERROR: a un insumo le falta la sección", error, file=sys.stderr)
    sys.exit(1)

# Las cuatro tablas por parroquia tienen que traer las mismas 1.240 parroquias, sin repetir
llaves = {}
for nombre, tabla in [("inamhi", inamhi), ("inundaciones", inundaciones), ("movimientos_masa", movimientos_masa),
                      ("mareas", mareas_parroquias)]:
    llaves[nombre] = set(zip(tabla["codpro"], tabla["codpar"]))
    if len(tabla) != TOTAL_PARROQUIAS or len(llaves[nombre]) != TOTAL_PARROQUIAS:
        print("ERROR:", nombre, "trae", len(tabla), "filas y", len(llaves[nombre]), "parroquias distintas", file=sys.stderr)
        sys.exit(1)
if len({frozenset(v) for v in llaves.values()}) != 1:
    print("ERROR: las tablas por parroquia no traen las mismas parroquias", file=sys.stderr)
    sys.exit(1)

faltan = [c for c in LLAVE + ["evento", "personasAfectadas", "personasDamnificadas"] if c not in eventos.columns]
if faltan:
    print("ERROR: a los eventos de Alístate les faltan campos:", faltan, file=sys.stderr)
    sys.exit(1)

# Eventos a más de 5 km de toda parroquia (Alístate los marca "sin parroquia"): se dejan fuera de la ficha con un aviso
sin_parroquia = eventos["codpro"].isna() | eventos["codpar"].isna()
if sin_parroquia.any():
    print("AVISO: se dejan fuera eventos sin parroquia:", eventos.loc[sin_parroquia, ["id_evento", "evento", "lat", "lon"]].values.tolist())
eventos = eventos[~sin_parroquia].reset_index(drop=True)

fuera = ~pd.Series(list(zip(eventos["codpro"], eventos["codpar"]))).isin(llaves["inundaciones"])
if fuera.any():
    print("ERROR: eventos de Alístate con una parroquia que no existe:", eventos.loc[fuera.values, LLAVE].values.tolist(), file=sys.stderr)
    sys.exit(1)

# ---------- 2. Eventos de Alístate por parroquia ----------

conteo = eventos.groupby(LLAVE + ["evento"]).size().rename("n").reset_index()
personas = eventos.groupby(LLAVE)[["personasAfectadas", "personasDamnificadas"]].sum().reset_index()
print("Eventos:", len(eventos), "en", len(personas), "parroquias")

# ---------- 3. Próximo aguaje de cada estación ----------

# Primer día de aguaje desde hoy (hora de Ecuador) y, en ese día, la pleamar más alta
pleamares["fecha"] = pleamares["fecha_hora"].str[:10]
hoy = ahora.strftime("%Y-%m-%d")
proximos = pleamares[pleamares["aguaje"] & (pleamares["fecha"] >= hoy)]
primer_dia = proximos.groupby("estacion")["fecha"].min().rename("primer_dia")
proximos = proximos.join(primer_dia, on="estacion")
proximos = proximos[proximos["fecha"] == proximos["primer_dia"]]
proximos = proximos.sort_values(["estacion", "altura_m", "fecha_hora"], ascending=[True, False, True]).drop_duplicates("estacion")
proximos = proximos.merge(estaciones[["id", "nombre"]].rename(columns={"id": "estacion", "nombre": "estacion_nombre"}), on="estacion")
print("Estaciones con próximo aguaje:", len(proximos), "de", len(estaciones))

aguajes = mareas_parroquias[mareas_parroquias["estacion"].notna()].merge(proximos, on="estacion")

# ---------- 4. Ficha por parroquia ----------

tabla = inundaciones[LLAVE + ["provincia", "canton", "parroquia", "zona_en_estudio", "categoria", "evaluacion_parcial"]].rename(
    columns={"categoria": "inundaciones", "evaluacion_parcial": "inundaciones_parcial"})
tabla = tabla.merge(movimientos_masa[LLAVE + ["categoria", "evaluacion_parcial"]].rename(
    columns={"categoria": "movimientos_masa", "evaluacion_parcial": "movimientos_masa_parcial"}), on=LLAVE)
tabla = tabla.merge(inamhi[LLAVE + ["nivel", "zona_propensa_crecidas"]], on=LLAVE)
tabla = tabla.merge(personas, on=LLAVE, how="left")
tabla = tabla.merge(aguajes[LLAVE + ["fecha_hora", "altura_m", "estacion", "estacion_nombre", "distancia_km"]], on=LLAVE, how="left")
tabla[["personasAfectadas", "personasDamnificadas"]] = tabla[["personasAfectadas", "personasDamnificadas"]].fillna(0).astype(int)
tabla = tabla.sort_values(["codpro", "canton", "parroquia"])

por_tipo = {}
for fila in conteo.itertuples():
    por_tipo.setdefault((fila.codpro, fila.codpar), {})[fila.evento] = int(fila.n)

registros = [
    {
        "codpro": r.codpro, "codpar": r.codpar, "provincia": r.provincia, "canton": r.canton, "parroquia": r.parroquia,
        "zona_en_estudio": bool(r.zona_en_estudio),
        "inamhi": {"nivel": r.nivel, "zona_propensa_crecidas": bool(r.zona_propensa_crecidas)},
        "inundaciones": {"categoria": r.inundaciones, "evaluacion_parcial": bool(r.inundaciones_parcial)},
        "movimientos_masa": {"categoria": r.movimientos_masa, "evaluacion_parcial": bool(r.movimientos_masa_parcial)},
        "eventos": {
            "total": sum(por_tipo.get((r.codpro, r.codpar), {}).values()),
            "por_tipo": por_tipo.get((r.codpro, r.codpar), {}),
            "personas_afectadas": int(r.personasAfectadas),
            "personas_damnificadas": int(r.personasDamnificadas),
        },
        "proximo_aguaje": {
            "fecha": r.fecha_hora[:10], "hora": r.fecha_hora[11:16], "altura_m": r.altura_m,
            "estacion": r.estacion, "estacion_nombre": r.estacion_nombre, "distancia_km": r.distancia_km,
        } if pd.notna(r.fecha_hora) else None,
    }
    for r in tabla.itertuples()
]

if len(registros) != TOTAL_PARROQUIAS or sum(x["eventos"]["total"] for x in registros) != len(eventos):
    print("ERROR: la ficha no cuadra con las parroquias o con el total de eventos", file=sys.stderr)
    sys.exit(1)

# ---------- 5. Salida ----------

salida = {
    "generado": ahora.isoformat(timespec="seconds"),
    "limites": "Parroquias del CNE (2025).",
    "fuentes": {
        "inamhi": {
            "fuente": insumos["inamhi"]["fuente"], "actualizado": insumos["inamhi"]["actualizado"],
            "estado": insumos["inamhi"]["estado"], "advertencias": insumos["inamhi"]["advertencias"],
        },
        "inundaciones": {"fuente": insumos["inundaciones"]["fuente"], "actualizado": insumos["inundaciones"]["generado"]},
        "movimientos_masa": {"fuente": insumos["movimientos_masa"]["fuente"], "actualizado": insumos["movimientos_masa"]["generado"]},
        "eventos": {"fuente": insumos["alistate"]["fuente"], "actualizado": insumos["alistate"]["actualizado"], "desde": "2026-08-29"},
        "mareas": {"fuente": insumos["mareas"]["fuente"], "actualizado": insumos["mareas"]["generado"], "periodo": insumos["mareas"]["periodo"]},
    },
    "total": len(registros),
    "parroquias": registros,
}

os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
temporal = SALIDA + ".tmp"
with open(temporal, "w", encoding="utf-8") as f:
    json.dump(salida, f, ensure_ascii=False, separators=(",", ":"))
os.replace(temporal, SALIDA)
print(SALIDA, os.path.getsize(SALIDA) // 1024, "KB;", sum(x["proximo_aguaje"] is not None for x in registros), "parroquias con próximo aguaje")
