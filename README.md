# Datos del especial El Niño 2026-2027 · Primicias

Este repositorio tiene los datos del especial de **Primicias** sobre el fenómeno de El Niño 2026-2027 en Ecuador. El equipo de datos de Primicias los reúne, procesa y publica a partir de fuentes oficiales, y alimentan la herramienta **Tu zona**: cada persona elige su parroquia y ve si hay una alerta de lluvia del INAMHI, qué tan susceptible es su territorio a inundaciones y a movimientos en masa, cuántas emergencias registró la Secretaría Nacional de Gestión de Riesgos y, en la costa, cuándo es el próximo aguaje.

Todos los datos están asignados a las **1.240 parroquias del Consejo Nacional Electoral (CNE), 2025**. Algunos se actualizan solos varias veces al día; otros son fijos.

## Cómo leer los datos

Los archivos se leen directamente desde GitHub, sin clave ni registro:

```
https://raw.githubusercontent.com/tefaceli/fenomeno_nino/main/<ruta del archivo>
```

- **Coordenadas:** EPSG:4326 (longitud, latitud).
- **Horas:** hora de Ecuador, con el desfase explícito (`-05:00`).
- **Parroquias:** la llave es `codpro` + `codpar`, los dos como texto con ceros a la izquierda. `codpar` solo no identifica una parroquia.
- **Codificación:** UTF-8. Los nombres de provincia, cantón y parroquia van como los publica el CNE: en mayúsculas y sin tildes.
- Cada archivo trae su fecha de actualización (`actualizado` o `generado`).

## Qué hay

| Dato | Archivo | Fuente | Se actualiza |
|---|---|---|---|
| Ficha de cada parroquia (todas las fuentes juntas) | `ficha/data/out/ficha_parroquias.json` | Varias | Con cada actualización de INAMHI o Alístate |
| Buscador de parroquias | `ficha/data/out/parroquias_indice.json` | CNE | Fijo |
| Límites de parroquias (versión liviana para mapas) | `parroquias/parroquias_2025.json` | CNE, 2025 | Fijo |
| Alertas de lluvia (mapa) | `inamhi/data/out/inamhi_advertencias.geojson` | INAMHI | 4 veces al día |
| Alertas de lluvia por parroquia | `inamhi/data/out/inamhi_parroquias.json` | INAMHI | 4 veces al día |
| Emergencias registradas desde el 29/08/2026 | `alistate/data/out/alistate_eventos.geojson` | SNGR (Alístate Ecuador) | 2 veces al día |
| Susceptibilidad a inundaciones (mapa y por parroquia) | `inundaciones/data/out/` | MAG y SGR, 2023 | Fijo |
| Susceptibilidad a movimientos en masa (mapa y por parroquia) | `mm/data/out/` | SGR, 2016 | Fijo |
| Mareas y aguajes (octubre de 2026 a marzo de 2027) | `marea/data/out/mareas.json` | INOCAR | Fijo |

## Cómo se actualiza

- Los datos que cambian se descargan y procesan con GitHub Actions (`.github/workflows/`): las alertas del INAMHI a las 00:00, 06:00, 12:00 y 18:00, y los eventos de Alístate a las 07:00 y 15:00 (hora de Ecuador). Después de cada corrida se vuelve a armar la ficha.
- Si una fuente no responde o trae errores, el archivo publicado no se toca: queda el último dato válido, con su fecha.
- Cada descarga se guarda comprimida y con fecha en `<fuente>/data/raw/`. Ese historial no se borra.

## Estructura

Una carpeta por fuente:

- `<fuente>/scripts/`: el script que descarga y procesa.
- `<fuente>/data/raw/`: historial de descargas.
- `<fuente>/data/out/`: los archivos publicados.

`ficha/` junta las fuentes en un solo archivo por parroquia. Los datos fijos (`inundaciones/`, `mm/`, `marea/`, `parroquias/`) se procesaron una sola vez y aquí solo está su resultado.

## Fuentes

- **Límites de parroquias:** Consejo Nacional Electoral (CNE), 2025.
- **Alertas de lluvia:** Instituto Nacional de Meteorología e Hidrología (INAMHI), feed oficial de alertas en formato CAP.
- **Emergencias:** Secretaría Nacional de Gestión de Riesgos (SNGR), visor Alístate Ecuador.
- **Susceptibilidad a inundaciones:** Ministerio de Agricultura y Ganadería (MAG) y Secretaría de Gestión de Riesgos, escala 1:25.000 (2023).
- **Susceptibilidad a movimientos en masa:** Secretaría de Gestión de Riesgos (SGR), escala 1:50.000 (2016).
- **Mareas:** Instituto Oceanográfico y Antártico de la Armada (INOCAR).

Las alertas, las cifras de emergencias y los mapas de susceptibilidad son de cada institución. La susceptibilidad describe qué tan propenso es un terreno por sus características físicas: no es un pronóstico ni mide el riesgo.

Recopilación y procesamiento: **Primicias**.
