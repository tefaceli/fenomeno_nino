# Descarga los eventos de El Niño del visor Alístate (SNGR), guarda el crudo con fecha
# y escribe alistate_eventos.geojson con la parroquia de cada evento según la capa del CNE.
# Si la descarga o la validación fallan, no se escribe nada y el script sale con código 1.

import os
import sys
import json
import gzip
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import geopandas as gpd

# ---------- Configuración ----------

EXEC_URL = "https://script.google.com/macros/s/AKfycbz6HA9kKLeksqm49YYPD7i3A40mMK6JwmoDc50vaiv4myVTk0lFYswqnE4IbJXmr9Lm/exec"
CALLBACK_URL = EXEC_URL.replace("/exec", "/callback")
HEADERS = {"User-Agent": "Primicias - especial El Nino"}
TIMEOUT = 60

CARPETA_FUENTE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA_RAIZ = os.path.dirname(CARPETA_FUENTE)
CARPETA_RAW = os.path.join(CARPETA_FUENTE, "data", "raw")
CARPETA_INTERIM = os.path.join(CARPETA_FUENTE, "data", "interim")
SALIDA = os.path.join(CARPETA_FUENTE, "data", "out", "alistate_eventos.geojson")
PARROQUIAS = os.path.join(CARPETA_RAIZ, "parroquias", "parroquias_cne_2025.geojson")  # capa CNE 2025 simplificada a 10 m en mapshaper

CAMPOS_OBLIGATORIOS = ["id", "fecha", "evento", "lat", "lon", "provincia", "canton", "parroquia", "estado"]
CAIDA_MAXIMA = 0.10  # si el total cae más de 10% respecto a la corrida anterior, se considera falla
# Eventos que se excluyen por error de origen. No se corrigen: si la SNGR los arregla, cambia su id_evento y vuelven solos.
# Se usa id_evento porque el id de Alístate se corre entre corridas.
EXCLUIDOS = {
    "d9d76871a0ce": "Caracol (Babahoyo, Los Ríos): latitud con signo invertido, cae en el mar",
}
DISTANCIA_MAXIMA_M = 5000  # eventos fuera de todo polígono (costa, ríos) se asignan a la parroquia más cercana hasta esta distancia

ECUADOR = timezone(timedelta(hours=-5))
ahora = datetime.now(ECUADOR)
print("Corrida:", ahora.isoformat(timespec="seconds"))

# ---------- 1. Descarga ----------

sesion = requests.Session()
sesion.headers.update(HEADERS)

argumentos = json.dumps([[{"n": "obtenerEventosElNino"}]], separators=(",", ":"))
peticion = json.dumps(["obtenerLote", argumentos, None, [0], None, None, 1, 0], separators=(",", ":"))

try:
    pagina = sesion.get(EXEC_URL, timeout=TIMEOUT)
    pagina.raise_for_status()
    respuesta = sesion.post(
        CALLBACK_URL,
        params={"nocache_id": "1"},
        data={"request": peticion},
        headers={"X-Same-Domain": "1", "Origin": "https://script.google.com", "Referer": EXEC_URL},
        timeout=TIMEOUT,
    )
    respuesta.raise_for_status()
except requests.RequestException as error:
    print("ERROR en la descarga:", error, file=sys.stderr)
    sys.exit(1)

texto = respuesta.text
print("Respuesta:", respuesta.status_code, "|", len(texto), "caracteres")

# ---------- 2. Validación ----------

try:
    externo = json.loads(texto[texto.index("[["):])
    interno = json.loads(externo[0][1][1])
    ok = interno[0]["ok"]
    eventos = interno[0]["v"]
except (ValueError, IndexError, KeyError, TypeError) as error:
    print("ERROR: la respuesta no tiene el formato esperado:", error, file=sys.stderr)
    print("Inicio de la respuesta:", texto[:300], file=sys.stderr)
    sys.exit(1)

if ok is not True or not isinstance(eventos, list) or len(eventos) == 0:
    print("ERROR: el visor no devolvió eventos (ok =", ok, ")", file=sys.stderr)
    print("Inicio de la respuesta:", texto[:300], file=sys.stderr)
    sys.exit(1)

df = pd.DataFrame(eventos)
faltan = [c for c in CAMPOS_OBLIGATORIOS if c not in df.columns]
if faltan:
    print("ERROR: faltan campos obligatorios:", faltan, file=sys.stderr)
    sys.exit(1)

if df["id"].duplicated().any():
    print("ERROR: hay ids repetidos:", df.loc[df["id"].duplicated(), "id"].tolist(), file=sys.stderr)
    sys.exit(1)

total_anterior = None
if os.path.exists(SALIDA):
    with open(SALIDA, encoding="utf-8") as f:
        total_anterior = json.load(f).get("total")
if total_anterior and len(df) < total_anterior * (1 - CAIDA_MAXIMA):
    print("ERROR: el total cayó de", total_anterior, "a", len(df), "eventos", file=sys.stderr)
    sys.exit(1)

print("Eventos recibidos:", len(df), "| corrida anterior:", total_anterior)

# ---------- 3. Crudo con fecha (historial) ----------

# Va comprimido en data/raw/AAAA/MM/ y se versiona en git: es el historial de todo lo que bajamos
carpeta_mes = os.path.join(CARPETA_RAW, ahora.strftime("%Y"), ahora.strftime("%m"))
os.makedirs(carpeta_mes, exist_ok=True)
archivo_raw = os.path.join(carpeta_mes, "raw_" + ahora.strftime("%Y-%m-%d_%H%M") + ".txt.gz")
with gzip.open(archivo_raw, "wt", encoding="utf-8") as f:
    f.write(texto)
print("Crudo guardado:", archivo_raw)

# ---------- 4. Limpieza ----------

# Coordenadas: numéricas y dentro de Ecuador (incluye Galápagos)
df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
dentro = df["lat"].between(-5.1, 1.7) & df["lon"].between(-92.1, -75.1)
if (~dentro).any():
    print("AVISO: se excluyen", (~dentro).sum(), "eventos sin coordenadas válidas:", df.loc[~dentro, "id"].tolist())
df = df[dentro].copy()

# Fecha en ISO con -05:00. Algunas vienen con "::" (ej. "06/10/2026 08::00")
fecha_texto = df["fecha"].astype(str).str.replace("::", ":", regex=False)
fecha = pd.to_datetime(fecha_texto, format="%d/%m/%Y %H:%M", errors="coerce")
if fecha.isna().any():
    print("AVISO: fechas que no se pudieron leer:", df.loc[fecha.isna(), ["id", "fecha"]].values.tolist())
df["fecha_iso"] = fecha.dt.strftime("%Y-%m-%dT%H:%M:00-05:00")

# "Varios sectores": una sola coordenada para muchos barrios
df["varios_sectores"] = df["nombre"].fillna("").str.lower().str.contains("varios sectores")

# id_evento: id propio y estable. El id de Alístate (NINO_n) es posicional y se corre cuando
# entra un evento con fecha atrasada. Si la SNGR edita fecha, coordenada, evento o parroquia, el id cambia.
llave = (fecha_texto + "|" + df["lat"].round(6).map("{:.6f}".format) + "|" + df["lon"].round(6).map("{:.6f}".format)
         + "|" + df["evento"].fillna("").astype(str) + "|" + df["parroquia"].fillna("").astype(str))
df["id_evento"] = [hashlib.sha1(k.encode("utf-8")).hexdigest()[:12] for k in llave]
df = df.rename(columns={"id": "id_alistate"})
df = df[["id_evento"] + [c for c in df.columns if c != "id_evento"]]

# Duplicados: mismo id_evento en la misma corrida. Se deja el primero y se guardan todos para revisión
duplicado = df["id_evento"].duplicated(keep=False)
os.makedirs(CARPETA_INTERIM, exist_ok=True)
df[duplicado].sort_values("id_evento").to_csv(os.path.join(CARPETA_INTERIM, "alistate_duplicados.csv"), index=False, encoding="utf-8-sig")
if duplicado.any():
    print("AVISO: se descartan", df["id_evento"].duplicated().sum(), "eventos duplicados (ver data/interim/alistate_duplicados.csv)")
df = df.drop_duplicates(subset="id_evento")

excluido = df["id_evento"].isin(EXCLUIDOS.keys())
if excluido.any():
    print("AVISO: se excluyen por error de origen:", df.loc[excluido, ["id_evento", "id_alistate"]].values.tolist())
df = df[~excluido]

# ---------- 5. Cruce con parroquias (CNE) ----------

parroquias = gpd.read_file(PARROQUIAS)[["CODPRO", "PROVINCIA", "CANTON", "CODPAR", "PARROQUIA", "geometry"]]
parroquias = parroquias.rename(columns={
    "CODPRO": "cne_codpro", "PROVINCIA": "cne_provincia", "CANTON": "cne_canton",
    "CODPAR": "cne_codpar", "PARROQUIA": "cne_parroquia",
})
parroquias = parroquias.to_crs(32717)
parroquias["geometry"] = parroquias.make_valid()  # la simplificación deja una autointersección mínima (Ciudad Nueva, Píllaro)

puntos = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs=4326).to_crs(32717)

cruce = gpd.sjoin(puntos, parroquias, how="left", predicate="intersects").drop(columns="index_right")
cruce = cruce.drop_duplicates(subset="id_evento")  # un punto justo en el límite puede tocar dos parroquias
cruce["cne_cruce"] = "dentro"

sin_parroquia = cruce["cne_codpar"].isna()
cercanos = gpd.sjoin_nearest(
    puntos[puntos["id_evento"].isin(cruce.loc[sin_parroquia, "id_evento"])], parroquias,
    how="left", max_distance=DISTANCIA_MAXIMA_M, distance_col="distancia_m",
).drop(columns="index_right").drop_duplicates(subset="id_evento")
cercanos["cne_cruce"] = "cercano"
cercanos.loc[cercanos["cne_codpar"].isna(), "cne_cruce"] = "sin parroquia"

cruce = pd.concat([cruce[~sin_parroquia], cercanos.drop(columns="distancia_m")])
cruce["zona_en_estudio"] = cruce["cne_codpar"] == "99"

print("Cruce:", cruce["cne_cruce"].value_counts().to_dict())
if cruce["zona_en_estudio"].any():
    print("AVISO: eventos en zona en estudio:", cruce.loc[cruce["zona_en_estudio"], "id_evento"].tolist())

# Revisión: parroquia de Alístate vs. parroquia del CNE (sin tildes, mayúsculas)
alistate_norm = cruce["parroquia"].fillna("").str.normalize("NFKD").str.encode("ascii", "ignore").str.decode("ascii").str.upper().str.strip()
cne_norm = cruce["cne_parroquia"].fillna("").str.normalize("NFKD").str.encode("ascii", "ignore").str.decode("ascii").str.upper().str.strip()
distinta = alistate_norm != cne_norm
revision = cruce.loc[distinta, ["id_evento", "id_alistate", "provincia", "canton", "parroquia", "cne_provincia", "cne_canton", "cne_parroquia", "cne_cruce", "lat", "lon"]]
revision.to_csv(os.path.join(CARPETA_INTERIM, "alistate_revision_parroquias.csv"), index=False, encoding="utf-8-sig")
print("Parroquias que no coinciden con el texto de Alístate:", len(revision), "(ver data/interim/alistate_revision_parroquias.csv)")

# ---------- 6. GeoJSON ----------

# La geometría se arma con las coordenadas originales para no arrastrar decimales de la reproyección
salida = gpd.GeoDataFrame(cruce.drop(columns="geometry"), geometry=gpd.points_from_xy(cruce["lon"], cruce["lat"]), crs=4326)
salida = salida.sort_values("fecha_iso", ascending=False)

# Manda la ubicación según los límites oficiales del CNE. El texto de la SNGR se conserva con sufijo _sngr
salida = salida.rename(columns={
    "provincia": "provincia_sngr", "canton": "canton_sngr", "parroquia": "parroquia_sngr", "ubicacion": "ubicacion_sngr",
})
salida = salida.rename(columns={
    "cne_codpro": "codpro", "cne_provincia": "provincia", "cne_canton": "canton",
    "cne_codpar": "codpar", "cne_parroquia": "parroquia", "cne_cruce": "cruce_parroquia",
})
geojson = json.loads(salida.to_json(na="null", drop_id=True))
geojson = {
    "type": "FeatureCollection",
    "fuente": "Alístate Ecuador (SNGR) - eventos El Niño desde 2026-08-29",
    "actualizado": ahora.isoformat(timespec="seconds"),
    "total": len(salida),
    "features": geojson["features"],
}

# Se escribe a un temporal y luego se reemplaza, para no dejar un archivo a medias
os.makedirs(os.path.dirname(SALIDA), exist_ok=True)
temporal = SALIDA + ".tmp"
with open(temporal, "w", encoding="utf-8") as f:
    json.dump(geojson, f, ensure_ascii=False, separators=(",", ":"))
os.replace(temporal, SALIDA)
print("GeoJSON escrito:", SALIDA, "|", len(salida), "eventos |", os.path.getsize(SALIDA) // 1024, "KB")
