# Descarga las advertencias meteorológicas del INAMHI (vía GeoGLOWS) vigentes o que empiezan en las
# próximas 24 horas, aplana los niveles de lluvia y calcula el nivel de advertencia de cada parroquia.
# Si la descarga o la validación fallan, no se escribe nada y el script sale con código 1.

import os
import sys
import json
import gzip
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon

# ---------- Configuración ----------

LISTA_URL = "https://services.geoglows.org/api/hydroviewer/get-meteo-warnings"
POLIGONOS_URL = "https://services.geoglows.org/api/met-data-explorer/get-meteo-warnings"
HEADERS = {"User-Agent": "Primicias - especial El Nino"}
TIMEOUT = 120

CARPETA_FUENTE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA_RAIZ = os.path.dirname(CARPETA_FUENTE)
CARPETA_RAW = os.path.join(CARPETA_FUENTE, "data", "raw")
SALIDA_MAPA = os.path.join(CARPETA_FUENTE, "data", "out", "inamhi_advertencias.geojson")
SALIDA_PARROQUIAS = os.path.join(CARPETA_FUENTE, "data", "out", "inamhi_parroquias.json")
PARROQUIAS = os.path.join(CARPETA_RAIZ, "parroquias", "parroquias_cne_2025.geojson")

HORIZONTE_HORAS = 24          # se toman las vigentes y las que empiezan en las próximas 24 horas
TIPO_LLUVIA = "Lluvia"        # solo advertencias de lluvia (se dejan fuera las de temperatura)
NIVELES = ["Medio", "Alto", "Muy Alto"]  # terminología oficial del INAMHI, de menor a mayor
UMBRAL = 10                   # % del área de la parroquia para asignarle un nivel (misma lógica que inundaciones)
SIMPLIFICACION_CALCULO_M = 100
SIMPLIFICACION_MAPA_M = 200
DECIMALES = 4

ECUADOR = timezone(timedelta(hours=-5))
ahora = datetime.now(ECUADOR)
ahora_local = ahora.replace(tzinfo=None)  # la API entrega horas sin zona, en hora de Ecuador
print("Corrida:", ahora.isoformat(timespec="seconds"))

sesion = requests.Session()
sesion.headers.update(HEADERS)

# ---------- 1. Lista de advertencias ----------

try:
    respuesta = sesion.get(LISTA_URL, timeout=TIMEOUT)
    respuesta.raise_for_status()
    texto_lista = respuesta.text
    lista = json.loads(texto_lista.replace("NaN", "null"))["result"]
except (requests.RequestException, ValueError, KeyError) as error:
    print("ERROR en la lista de advertencias:", error, file=sys.stderr)
    sys.exit(1)

lista = pd.DataFrame(lista)
faltan = [c for c in ["warning", "type", "start", "end"] if c not in lista.columns]
if len(lista) == 0 or faltan:
    print("ERROR: la lista viene vacía o le faltan campos:", faltan, file=sys.stderr)
    sys.exit(1)

lista["inicio"] = pd.to_datetime(lista["start"], errors="coerce")
lista["fin"] = pd.to_datetime(lista["end"], errors="coerce")
if lista[["inicio", "fin"]].isna().any().any():
    print("ERROR: fechas que no se pudieron leer:", lista.loc[lista["inicio"].isna() | lista["fin"].isna()].values.tolist(), file=sys.stderr)
    sys.exit(1)

limite = ahora_local + timedelta(hours=HORIZONTE_HORAS)
en_ventana = (lista["inicio"] <= limite) & (lista["fin"] >= ahora_local)
de_lluvia = lista["type"].str.contains(TIPO_LLUVIA, case=False)
if (en_ventana & ~de_lluvia).any():
    print("AVISO: se dejan fuera advertencias que no son de lluvia:", lista.loc[en_ventana & ~de_lluvia, ["warning", "type"]].values.tolist())
seleccion = lista[en_ventana & de_lluvia].copy()
seleccion["estado"] = "próxima"
seleccion.loc[seleccion["inicio"] <= ahora_local, "estado"] = "vigente"
print("Advertencias en la lista:", len(lista), "| seleccionadas:", seleccion[["warning", "type", "start", "end", "estado"]].values.tolist())

# ---------- 2. Polígonos de cada advertencia seleccionada ----------

partes = []
for _, adv in seleccion.iterrows():
    try:
        r = sesion.get(POLIGONOS_URL, params={"no": adv["warning"]}, timeout=TIMEOUT)
        r.raise_for_status()
        texto = r.text
        features = json.loads(texto.replace("NaN", "null"))["features"]
    except (requests.RequestException, ValueError, KeyError) as error:
        print("ERROR en los polígonos de la advertencia", adv["warning"], ":", error, file=sys.stderr)
        sys.exit(1)

    # El mismo número puede repetirse con otras fechas: se toman solo los polígonos de este periodo
    propias = [f for f in features if f["properties"].get("start") == adv["start"] and f["properties"].get("end") == adv["end"]]
    if len(propias) == 0:
        print("ERROR: la advertencia", adv["warning"], "no trae polígonos para", adv["start"], "-", adv["end"], file=sys.stderr)
        sys.exit(1)

    # Historial: cada advertencia se guarda una sola vez (sus polígonos no cambian)
    carpeta = os.path.join(CARPETA_RAW, "advertencias", adv["inicio"].strftime("%Y"))
    archivo = os.path.join(carpeta, "advertencia_" + adv["warning"] + "_" + adv["inicio"].strftime("%Y-%m-%d_%H%M") + ".geojson.gz")
    os.makedirs(carpeta, exist_ok=True)
    if not os.path.exists(archivo):
        with gzip.open(archivo, "wt", encoding="utf-8") as f:
            f.write(texto)
        print("Polígonos guardados:", archivo)

    partes.append(gpd.GeoDataFrame.from_features(propias, crs=4326))
    print("Advertencia", adv["warning"], "|", len(propias), "polígonos |", pd.Series([f["properties"]["level"] for f in propias]).value_counts().to_dict())

# ---------- 3. Historial de la lista ----------

carpeta_mes = os.path.join(CARPETA_RAW, ahora.strftime("%Y"), ahora.strftime("%m"))
os.makedirs(carpeta_mes, exist_ok=True)
archivo_lista = os.path.join(carpeta_mes, "lista_" + ahora.strftime("%Y-%m-%d_%H%M") + ".json.gz")
with gzip.open(archivo_lista, "wt", encoding="utf-8") as f:
    f.write(texto_lista)
print("Lista guardada:", archivo_lista)

# ---------- 4. Aplanar niveles ----------

poligonos = gpd.GeoDataFrame(pd.concat(partes, ignore_index=True), crs=4326) if partes else gpd.GeoDataFrame({"level": []}, geometry=[], crs=4326)
poligonos = poligonos.to_crs(32717)
poligonos["geometry"] = poligonos.make_valid()

# Los polígonos vienen duplicados: se deduplican por nivel + geometría
poligonos["wkb"] = poligonos.geometry.to_wkb()
duplicados = poligonos.duplicated(["level", "wkb"]).sum()
poligonos = poligonos.drop_duplicates(["level", "wkb"])
otros = sorted(set(poligonos["level"]) - set(NIVELES) - {"Crecida"})
if otros:
    print("AVISO: niveles que no se usan:", otros)
print("Polígonos:", len(poligonos), "| duplicados descartados:", duplicados)

# Unión por nivel (también junta varias advertencias a la vez). Si un nivel no está, queda vacío
union = poligonos.dissolve(by="level")["geometry"].reindex(NIVELES + ["Crecida"]).fillna(Polygon())

# Los niveles vienen apilados (Medio debajo, Alto y Muy Alto encima): cada zona queda solo con su nivel más alto
muy_alto = union["Muy Alto"]
alto = union["Alto"].difference(muy_alto)
medio = union["Medio"].difference(union["Alto"]).difference(muy_alto)

capas = gpd.GeoDataFrame(
    {"nivel": ["Medio", "Alto", "Muy Alto", "Zona propensa a crecidas"], "capa": ["lluvia", "lluvia", "lluvia", "crecidas"]},
    geometry=[medio, alto, muy_alto, union["Crecida"]], crs=32717,
)
capas = capas[~capas.is_empty]
print("km² por nivel:", dict(zip(capas["nivel"], (capas.area / 1e6).round(0))))

# ---------- 5. Nivel por parroquia ----------

parroquias = gpd.read_file(PARROQUIAS).to_crs(32717)
parroquias["geometry"] = parroquias.make_valid()
parroquias["km2"] = parroquias.area / 1e6

calculo = capas.copy()
calculo["geometry"] = calculo.simplify(SIMPLIFICACION_CALCULO_M)
cruce = gpd.overlay(parroquias[["CODPRO", "CODPAR", "geometry"]], calculo, how="intersection", keep_geom_type=True)
cruce["km2"] = cruce.area / 1e6
km2 = cruce.pivot_table(index=["CODPRO", "CODPAR"], columns="nivel", values="km2", aggfunc="sum").reindex(columns=NIVELES + ["Zona propensa a crecidas"])
km2.columns = ["medio", "alto", "muy_alto", "crecidas"]

tabla = parroquias[["CODPRO", "PROVINCIA", "CANTON", "CODPAR", "PARROQUIA", "km2"]].merge(km2.reset_index(), on=["CODPRO", "CODPAR"], how="left")
for columna in ["medio", "alto", "muy_alto", "crecidas"]:
    tabla["pct_" + columna] = (100 * tabla[columna].fillna(0) / tabla["km2"]).clip(upper=100)

# Misma lógica que inundaciones: el nivel más alto que, sumado a los superiores, cubre al menos el 10 % de la parroquia
tabla["nivel"] = "Sin advertencia"
tabla.loc[tabla["pct_medio"] + tabla["pct_alto"] + tabla["pct_muy_alto"] >= UMBRAL, "nivel"] = "Medio"
tabla.loc[tabla["pct_alto"] + tabla["pct_muy_alto"] >= UMBRAL, "nivel"] = "Alto"
tabla.loc[tabla["pct_muy_alto"] >= UMBRAL, "nivel"] = "Muy Alto"
tabla["zona_propensa_crecidas"] = tabla["pct_crecidas"] >= UMBRAL
print("Parroquias por nivel:", tabla["nivel"].value_counts().to_dict(), "| con zona propensa a crecidas:", int(tabla["zona_propensa_crecidas"].sum()))

# ---------- 6. Salidas ----------

advertencias = [
    {"numero": a.warning, "tipo": a.type, "inicio": a.inicio.strftime("%Y-%m-%dT%H:%M:00-05:00"), "fin": a.fin.strftime("%Y-%m-%dT%H:%M:00-05:00"), "estado": a.estado}
    for a in seleccion.itertuples()
]
metadatos = {
    "fuente": "Advertencias meteorológicas del INAMHI, obtenidas de GeoGLOWS (services.geoglows.org).",
    "actualizado": ahora.isoformat(timespec="seconds"),
    "estado": "con advertencias" if advertencias else "sin advertencias",
    "criterio": "Advertencias de lluvia vigentes o que empiezan en las próximas " + str(HORIZONTE_HORAS) + " horas. Horas de la fuente en hora de Ecuador.",
    "advertencias": advertencias,
}

# Mapa
mapa = capas.copy()
mapa["geometry"] = mapa.simplify(SIMPLIFICACION_MAPA_M)
mapa = mapa.to_crs(4326)
os.makedirs(os.path.dirname(SALIDA_MAPA), exist_ok=True)
temporal = SALIDA_MAPA + ".tmp"
mapa.to_file(temporal, driver="GeoJSON", COORDINATE_PRECISION=DECIMALES)
with open(temporal, encoding="utf-8") as f:
    features = json.load(f)["features"]
geojson = {"type": "FeatureCollection"}
geojson.update(metadatos)
geojson["features"] = features
with open(temporal, "w", encoding="utf-8") as f:
    json.dump(geojson, f, ensure_ascii=False, separators=(",", ":"))

# Parroquias
registros = [
    {
        "codpro": r.CODPRO, "codpar": r.CODPAR, "provincia": r.PROVINCIA, "canton": r.CANTON, "parroquia": r.PARROQUIA,
        "nivel": r.nivel,
        "pct_area": {"muy_alto": round(r.pct_muy_alto, 1), "alto": round(r.pct_alto, 1), "medio": round(r.pct_medio, 1)},
        "zona_propensa_crecidas": bool(r.zona_propensa_crecidas),
        "pct_area_crecidas": round(r.pct_crecidas, 1),
    }
    for r in tabla.sort_values(["CODPRO", "CANTON", "PARROQUIA"]).itertuples()
]
salida_parroquias = dict(metadatos)
salida_parroquias["regla"] = "Nivel más alto que, sumado a los niveles superiores, cubre al menos el " + str(UMBRAL) + " % del área de la parroquia. Zona propensa a crecidas: al menos el " + str(UMBRAL) + " % del área."
salida_parroquias["total"] = len(registros)
salida_parroquias["parroquias"] = registros
temporal_parroquias = SALIDA_PARROQUIAS + ".tmp"
with open(temporal_parroquias, "w", encoding="utf-8") as f:
    json.dump(salida_parroquias, f, ensure_ascii=False, separators=(",", ":"))

# Se reemplazan los dos archivos al final, cuando ambos están completos
os.replace(temporal, SALIDA_MAPA)
os.replace(temporal_parroquias, SALIDA_PARROQUIAS)
print("Mapa:", SALIDA_MAPA, "|", os.path.getsize(SALIDA_MAPA) // 1024, "KB")
print("Parroquias:", SALIDA_PARROQUIAS, "|", os.path.getsize(SALIDA_PARROQUIAS) // 1024, "KB")
