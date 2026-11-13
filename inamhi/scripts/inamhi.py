# Descarga las alertas meteorológicas del INAMHI desde su feed CAP oficial, toma las de lluvia vigentes o
# que empiezan en las próximas 24 horas, aplana los niveles y calcula el nivel de cada parroquia.
# Si la descarga o la lectura fallan, no se escribe nada (ni salidas ni historial) y el script sale con
# código 1. Si aparece una señal de que el INAMHI cambió su escala, se publica igual y se deja un aviso
# para que el workflow salga en rojo al final.

import os
import sys
import json
import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon

# ---------- Configuración ----------

RSS_URL = "https://cap-sources.s3.amazonaws.com/ec-inamhi-es/rss.xml"
NS = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
HEADERS = {"User-Agent": "Primicias - especial El Nino"}
TIMEOUT = 120

CARPETA_FUENTE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA_RAIZ = os.path.dirname(CARPETA_FUENTE)
CARPETA_RAW = os.path.join(CARPETA_FUENTE, "data", "raw")
CARPETA_CAP = os.path.join(CARPETA_RAW, "cap")
SALIDA_MAPA = os.path.join(CARPETA_FUENTE, "data", "out", "inamhi_advertencias.geojson")
SALIDA_PARROQUIAS = os.path.join(CARPETA_FUENTE, "data", "out", "inamhi_parroquias.json")
PARROQUIAS = os.path.join(CARPETA_RAIZ, "parroquias", "parroquias_cne_2025.geojson")
# En GitHub Actions el aviso va a la carpeta temporal del runner; en local, a data/interim (fuera de git)
ARCHIVO_REVISION = os.path.join(os.environ.get("RUNNER_TEMP", os.path.join(CARPETA_FUENTE, "data", "interim")), "inamhi_revisar.txt")

HORIZONTE_HORAS = 24          # se toman las vigentes y las que empiezan en las próximas 24 horas
FILTRO_EVENTO = "lluvia|tormenta"  # solo alertas de lluvia (se dejan fuera las de temperatura)
# Escala del INAMHI ("Los colores de la lluvia"), de menos a más lluvia, y sus llaves en pct_area
NIVELES = ["Llovizna", "Lluvia", "Lluvias fuertes", "Tormenta"]
LLAVES = ["llovizna", "lluvia", "lluvias_fuertes", "tormenta"]
# Severidad CAP -> nivel. Comprobado con las advertencias 84 a 87. Tormenta todavía no tiene equivalencia.
EQUIVALENCIA = {"Moderate": "Llovizna", "Severe": "Lluvia", "Extreme": "Lluvias fuertes"}
# Severidades no vistas: se publican con el nivel más cercano y se pide revisión. Unknown se descarta.
CERCANA = {"Minor": "Llovizna"}
EVENTOS_CONOCIDOS = ["lluvia tormenta eléctrica"]
SEVERIDADES_MAXIMAS = 3       # hoy cada advertencia usa como mucho tres severidades
UMBRAL = 10                   # % del área de la parroquia para asignarle un nivel (misma lógica que inundaciones)
SIMPLIFICACION_CALCULO_M = 100
SIMPLIFICACION_MAPA_M = 200
DECIMALES = 4

ECUADOR = timezone(timedelta(hours=-5))
ahora = datetime.now(ECUADOR)
print("Corrida:", ahora.isoformat(timespec="seconds"))

if os.path.exists(ARCHIVO_REVISION):
    os.remove(ARCHIVO_REVISION)
revisar = []

sesion = requests.Session()
sesion.headers.update(HEADERS)

# ---------- 1. Feed RSS ----------

try:
    respuesta = sesion.get(RSS_URL, timeout=TIMEOUT)
    respuesta.raise_for_status()
    crudo_rss = respuesta.content
    enlaces = [item.findtext("link") for item in ET.fromstring(crudo_rss).iter("item")]
except (requests.RequestException, ET.ParseError) as error:
    print("ERROR en el feed RSS:", error, file=sys.stderr)
    sys.exit(1)

if len(enlaces) == 0 or None in enlaces:
    print("ERROR: el feed RSS viene vacío o con alertas sin enlace", file=sys.stderr)
    sys.exit(1)
print("Alertas en el feed:", len(enlaces))

# ---------- 2. Cada alerta (XML CAP) ----------

# Los XML no cambian: si ya están en el historial se leen de ahí y no se vuelven a bajar
nuevos = {}
alertas = []
filas = []
for enlace in enlaces:
    nombre = enlace.split("/")[-1]
    archivo = os.path.join(CARPETA_CAP, nombre[:4], nombre[5:7], nombre + ".gz")
    try:
        crudo = gzip.open(archivo, "rb").read() if os.path.exists(archivo) else sesion.get(enlace, timeout=TIMEOUT).content
        alerta = ET.fromstring(crudo)
    except (requests.RequestException, OSError, ET.ParseError) as error:
        print("ERROR en la alerta", nombre, ":", error, file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(archivo):
        nuevos[archivo] = crudo

    # Si S3 devuelve un error (también es XML) o la alerta viene incompleta, no es una alerta CAP válida
    info = alerta.find("cap:info", NS)
    if alerta.tag != "{" + NS["cap"] + "}alert" or info is None:
        print("ERROR: el archivo", nombre, "no es una alerta CAP válida:", crudo[:200], file=sys.stderr)
        sys.exit(1)
    poligonos = [p.text for area in info.findall("cap:area", NS) for p in area.findall("cap:polygon", NS)]
    alertas.append({
        "archivo": nombre,
        "identificador": alerta.findtext("cap:identifier", namespaces=NS),
        "status": alerta.findtext("cap:status", namespaces=NS),
        "msgType": alerta.findtext("cap:msgType", namespaces=NS),
        "references": alerta.findtext("cap:references", default="", namespaces=NS),
        "evento": info.findtext("cap:event", default="", namespaces=NS),
        "severidad": info.findtext("cap:severity", default="", namespaces=NS),
        "titular": info.findtext("cap:headline", default="", namespaces=NS).strip(),
        # Si falta onset se usa effective y, si no, la fecha de envío
        "inicio_texto": info.findtext("cap:onset", namespaces=NS) or info.findtext("cap:effective", namespaces=NS) or alerta.findtext("cap:sent", namespaces=NS),
        "fin_texto": info.findtext("cap:expires", namespaces=NS),
        "poligonos": len(poligonos),
    })
    # CAP escribe los vértices como "lat,lon"; GeoJSON va como (lon, lat)
    try:
        for texto in poligonos:
            vertices = [(float(par.split(",")[1]), float(par.split(",")[0])) for par in texto.split()]
            filas.append({"archivo": nombre, "geometry": Polygon(vertices)})
    except (ValueError, IndexError) as error:
        print("ERROR: polígono ilegible en la alerta", nombre, ":", error, file=sys.stderr)
        sys.exit(1)

alertas = pd.DataFrame(alertas)
alertas["inicio"] = pd.to_datetime(alertas["inicio_texto"], utc=True, errors="coerce").dt.tz_convert(ECUADOR)
alertas["fin"] = pd.to_datetime(alertas["fin_texto"], utc=True, errors="coerce").dt.tz_convert(ECUADOR)
if alertas[["inicio", "fin"]].isna().any().any():
    print("ERROR: fechas que no se pudieron leer:", alertas.loc[alertas["inicio"].isna() | alertas["fin"].isna(), ["archivo", "inicio_texto", "fin_texto"]].values.tolist(), file=sys.stderr)
    sys.exit(1)

# ---------- 3. Selección ----------

# Se descartan pruebas, cancelaciones y las alertas que otra posterior actualiza o cancela.
# references trae "emisor,identificador,fecha" separados por espacios.
reemplazadas = set(r.split(",")[1] for refs in alertas["references"] for r in refs.split() if len(r.split(",")) == 3)
validas = (alertas["status"] == "Actual") & alertas["msgType"].isin(["Alert", "Update"]) & ~alertas["identificador"].isin(reemplazadas)
print("Alertas descartadas (pruebas, cancelaciones o reemplazadas):", int((~validas).sum()))

limite = ahora + timedelta(hours=HORIZONTE_HORAS)
en_ventana = validas & (alertas["inicio"] <= limite) & (alertas["fin"] >= ahora)
de_lluvia = alertas["evento"].str.contains(FILTRO_EVENTO, case=False)
if (en_ventana & ~de_lluvia).any():
    print("AVISO: se dejan fuera alertas que no son de lluvia:", alertas.loc[en_ventana & ~de_lluvia, ["archivo", "evento"]].values.tolist())
seleccion = alertas[en_ventana & de_lluvia].copy()

# Nivel según la severidad. Las desconocidas se publican con el nivel más cercano (o se descartan) y se pide revisión
seleccion["nivel"] = seleccion["severidad"].map(EQUIVALENCIA).fillna(seleccion["severidad"].map(CERCANA))
desconocidas = ~seleccion["severidad"].isin(EQUIVALENCIA.keys())
revisar += ["Severidad sin equivalencia confirmada: '" + s + "' (" + a + "), publicada como: " + n for a, s, n in seleccion.loc[desconocidas, ["archivo", "severidad", "nivel"]].fillna("no se publicó").values]
revisar += ["Tipo de evento nuevo: '" + e + "' (" + a + ")" for a, e in seleccion.loc[~seleccion["evento"].str.lower().str.strip().isin(EVENTOS_CONOCIDOS), ["archivo", "evento"]].values]
revisar += ["Alerta sin polígonos: " + a for a in seleccion.loc[seleccion["poligonos"] == 0, "archivo"]]
severidades = seleccion.groupby(["inicio", "fin", "titular"])["severidad"].nunique()
revisar += ["Advertencia con " + str(n) + " severidades distintas: " + t + " (" + str(i) + ")" for (i, f, t), n in severidades[severidades > SEVERIDADES_MAXIMAS].items()]
seleccion = seleccion[seleccion["nivel"].notna()]

seleccion["estado"] = "próxima"
seleccion.loc[seleccion["inicio"] <= ahora, "estado"] = "vigente"
print("Alertas seleccionadas:", seleccion[["archivo", "severidad", "nivel", "estado"]].values.tolist())

# ---------- 4. Aplanar niveles ----------

poligonos = gpd.GeoDataFrame(filas, columns=["archivo", "geometry"], geometry="geometry", crs=4326)
poligonos = poligonos.merge(seleccion[["archivo", "nivel"]], on="archivo").to_crs(32717)
poligonos["geometry"] = poligonos.make_valid()

# Se deduplican por nivel + geometría por si el INAMHI repite un polígono
poligonos["wkb"] = poligonos.geometry.to_wkb()
duplicados = poligonos.duplicated(["nivel", "wkb"]).sum()
poligonos = poligonos.drop_duplicates(["nivel", "wkb"])
print("Polígonos:", len(poligonos), "| duplicados descartados:", duplicados)

# Unión por nivel (también junta varias advertencias a la vez). Si un nivel no está, queda vacío
union = poligonos.dissolve(by="nivel")["geometry"].reindex(NIVELES).fillna(Polygon())

# Los niveles vienen apilados: cada zona queda solo con su nivel más alto
tormenta = union["Tormenta"]
lluvias_fuertes = union["Lluvias fuertes"].difference(tormenta)
lluvia = union["Lluvia"].difference(union["Lluvias fuertes"]).difference(tormenta)
llovizna = union["Llovizna"].difference(union["Lluvia"]).difference(union["Lluvias fuertes"]).difference(tormenta)

capas = gpd.GeoDataFrame(
    {"nivel": NIVELES, "capa": ["lluvia"] * len(NIVELES)},
    geometry=[llovizna, lluvia, lluvias_fuertes, tormenta], crs=32717,
)
capas = capas[~capas.is_empty]
print("km² por nivel:", dict(zip(capas["nivel"], (capas.area / 1e6).round(0))))

# ---------- 5. Nivel por parroquia ----------

parroquias = gpd.read_file(PARROQUIAS).to_crs(32717)
parroquias["geometry"] = parroquias.make_valid()
parroquias["km2"] = parroquias.area / 1e6

calculo = capas.copy()
calculo["geometry"] = calculo.simplify(SIMPLIFICACION_CALCULO_M)
vacio = gpd.GeoDataFrame({"CODPRO": [], "CODPAR": [], "nivel": []}, geometry=[], crs=32717)
cruce = gpd.overlay(parroquias[["CODPRO", "CODPAR", "geometry"]], calculo, how="intersection", keep_geom_type=True) if len(calculo) else vacio
cruce["km2"] = cruce.area / 1e6

tabla = parroquias[["CODPRO", "PROVINCIA", "CANTON", "CODPAR", "PARROQUIA", "km2"]].copy()
for nivel, llave in zip(NIVELES, LLAVES):
    km2 = cruce[cruce["nivel"] == nivel].groupby(["CODPRO", "CODPAR"])["km2"].sum().rename(llave)
    tabla = tabla.merge(km2, left_on=["CODPRO", "CODPAR"], right_index=True, how="left")
    tabla["pct_" + llave] = (100 * tabla[llave].fillna(0) / tabla["km2"]).clip(upper=100)

# Misma lógica que inundaciones: el nivel más alto que, sumado a los superiores, cubre al menos el 10 % de la parroquia
tabla["nivel"] = "Sin advertencia"
for i, nivel in enumerate(NIVELES):
    tabla.loc[tabla[["pct_" + llave for llave in LLAVES[i:]]].sum(axis=1) >= UMBRAL, "nivel"] = nivel
print("Parroquias por nivel:", tabla["nivel"].value_counts().to_dict())

# ---------- 6. Salidas ----------

# Una advertencia = alertas con el mismo inicio, fin y titular. El feed CAP no trae el número oficial.
grupos = seleccion.groupby(["inicio", "fin", "titular"], as_index=False)["estado"].first().sort_values("inicio")
advertencias = [
    {"numero": None, "tipo": a.titular, "inicio": a.inicio.strftime("%Y-%m-%dT%H:%M:00-05:00"), "fin": a.fin.strftime("%Y-%m-%dT%H:%M:00-05:00"), "estado": a.estado}
    for a in grupos.itertuples()
]
metadatos = {
    "fuente": "Alertas meteorológicas del INAMHI, feed CAP oficial (cap-sources.s3.amazonaws.com/ec-inamhi-es).",
    "actualizado": ahora.isoformat(timespec="seconds"),
    "estado": "con advertencias" if advertencias else "sin advertencias",
    "criterio": "Alertas de lluvia vigentes o que empiezan en las próximas " + str(HORIZONTE_HORAS) + " horas. Horas en hora de Ecuador.",
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
        "codpro": r["CODPRO"], "codpar": r["CODPAR"], "provincia": r["PROVINCIA"], "canton": r["CANTON"], "parroquia": r["PARROQUIA"],
        "nivel": r["nivel"],
        "pct_area": {llave: round(r["pct_" + llave], 1) for llave in LLAVES},
    }
    for _, r in tabla.sort_values(["CODPRO", "CANTON", "PARROQUIA"]).iterrows()
]
salida_parroquias = dict(metadatos)
salida_parroquias["regla"] = "Nivel más alto que, sumado a los niveles superiores, cubre al menos el " + str(UMBRAL) + " % del área de la parroquia. Escala del INAMHI de menos a más lluvia: " + ", ".join(NIVELES) + "."
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

# ---------- 7. Historial (solo si todo salió bien) ----------

# Cada alerta CAP se guarda una sola vez; el RSS, en cada corrida
for archivo, crudo in nuevos.items():
    os.makedirs(os.path.dirname(archivo), exist_ok=True)
    with gzip.open(archivo, "wb") as f:
        f.write(crudo)
print("Alertas nuevas guardadas en el historial:", len(nuevos))
carpeta_mes = os.path.join(CARPETA_RAW, ahora.strftime("%Y"), ahora.strftime("%m"))
os.makedirs(carpeta_mes, exist_ok=True)
archivo_rss = os.path.join(carpeta_mes, "rss_" + ahora.strftime("%Y-%m-%d_%H%M") + ".xml.gz")
with gzip.open(archivo_rss, "wb") as f:
    f.write(crudo_rss)
print("RSS guardado:", archivo_rss)

# ---------- 8. Aviso de revisión ----------

# Los datos ya quedaron publicados. Si hay algo que revisar, el workflow sale en rojo en su último paso.
if revisar:
    os.makedirs(os.path.dirname(ARCHIVO_REVISION), exist_ok=True)
    with open(ARCHIVO_REVISION, "w", encoding="utf-8") as f:
        f.write("\n".join(revisar) + "\n")
    print("REVISAR (posible cambio en la escala del INAMHI):")
    print("\n".join("::warning::" + r for r in revisar))
