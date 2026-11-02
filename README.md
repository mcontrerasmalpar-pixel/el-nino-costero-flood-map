# Mapa de riesgo de inundacion - El Nino Costero 2026-2027 (Peru)

Mapa interactivo (Leaflet) que combina el historial de emergencias por lluvias e inundaciones en Peru, los puntos criticos validados por CENEPRED y condiciones en vivo de NASA GIBS. Es una sola pagina estatica (`data/index.html`), sin backend.

## Capas

Historico (archivos estaticos):

1. Historial nacional de inundaciones: mapa de calor por distrito, ponderado por numero de eventos. Apagado por defecto.
2. Zona El Nino Costero: mapa de calor de Tumbes, Piura, Lambayeque y La Libertad, con tooltips de tasas por 1,000 habitantes.
3. Puntos criticos CENEPRED: marcadores con paraje, distrito, rio, familias expuestas y enlace al PDF fuente.

Condiciones en vivo (NASA GIBS, se regeneran a diario):

4. Anomalia de temperatura superficial del mar (MUR SST v4.1). Latencia de unos 4 dias; la pagina pide la fecha de hoy menos 5 dias.
5. Precipitacion (IMERG). Latencia de aproximadamente 1 dia; la pagina pide la fecha de hoy menos 2 dias.

## Ejecucion local

La pagina carga los GeoJSON con `fetch()`, por lo que debe servirse por HTTP. Abrirla como `file://` no funciona.

```
cd data
python -m http.server 8000
```

Luego abrir http://localhost:8000/index.html

El mapa base es Esri World Dark Gray (gratuito, sin clave). No se requiere ninguna API key.

## Estado ENFEN (franja superior)

`data/enfen_status.json` guarda el estado y el ultimo comunicado oficial ENFEN (numero, fecha, enlace). Es informacion fechada: ENFEN publica cada ~2 semanas, asi que hay que actualizar ese archivo con cada comunicado. Si la fecha tiene mas de 21 dias, la pagina lo marca como posiblemente desactualizado. El mapa no es un aviso oficial (ver SENAMHI / INDECI).

## Colores

La escala de tasas y los mapas de calor usan solo azules/cian (secuencial frio). No se usan amarillo, naranja, rojo, morado ni negro porque en Peru son los colores oficiales de alerta (SENAMHI). Distritos con poblacion < 2,000 se muestran atenuados; "sin datos" es un anillo punteado sin relleno.

## Archivos de datos

Los archivos pequenos necesarios para el mapa estan incluidos en `data/`:

- `emergencias_por_distrito.geojson`
- `emergencias_distrito_costa_nino.geojson`
- `puntos_criticos_inundacion.geojson`
- `population_por_distrito.json`

Los archivos grandes se excluyen del repositorio (`.gitignore`) porque superan o se acercan al limite de GitHub y se regeneran con los scripts:

- `registro_emergencias_indeci.geojson` (aprox. 186 MB, registro crudo)
- `emergencias_inundacion_filtered.geojson` (aprox. 76 MB, eventos filtrados)
- `pobreza2018.rda` (fuente intermedia de poblacion)

## Pipeline de datos

Ejecutar en este orden desde la raiz del proyecto (requiere Python 3, `requests`, `pandas` y `pyreadr`):

1. `fetch_data.py`: descarga las capas de puntos criticos (capa 3) y registro de emergencias (capa 2) desde GeoServidorPeru (MINAM).
2. `process_data.py`: filtra a fenomenos de lluvia e inundacion, agrega por distrito y genera la capa de la costa norte.
3. `build_population_from_inei.py`: genera `population_por_distrito.json` a partir de `pobreza2018.rda`. Descargar antes ese archivo desde https://github.com/calderonsamuel/perupobreza2018 (`data/pobreza2018.rda`) y guardarlo en `data/`.
4. `add_population_rates.py`: agrega `poblacion_2020`, `event_rate_per_1000` y `afectados_rate_per_1000` a las capas por distrito.

`fetch_population.py` se conserva solo como referencia: el servicio de CENEPRED que consultaba ya no existe.

## Fuentes

- INDECI / SINPAD, registro de emergencias (via GeoServidorPeru, MINAM).
- CENEPRED / SIGRID, puntos criticos por inundacion.
- INEI, Mapa de Pobreza Monetaria 2018 (campo `pob_2020`), tomado del paquete `perupobreza2018`.
- NASA GIBS: GHRSST MUR SST v4.1 e IMERG.
- Mapa base: Esri.

## Limitaciones y metodologia

- El historial cubre 2003 a 2021-12. Es una linea base historica, no un reporte en vivo del evento 2026-2027.
- Las tasas por 1,000 habitantes son sumas acumuladas de unos 19 anos de eventos distintos, divididas entre la poblacion; no son un porcentaje en un momento dado. Distritos pequenos pueden superar 1,000 por mil. La escala de color es logaritmica y cada capa tiene su propio tope (Costa Norte: su maximo real, ~31 por mil; nacional: 1,000 por mil), por lo que los colores no son comparables entre capas, y los mapas de calor usan peso logaritmico para que los valores extremos no dominen.
- La poblacion es una proyeccion INEI a 2020, no a 2026. Los distritos urbanos de rapido crecimiento quedan subestimados. Ocho distritos de la capa nacional no tienen poblacion asociada y sus tasas son nulas.
- La zona El Nino Costero es un filtro geografico (cuatro departamentos del norte), no un filtro por anos con El Nino.
