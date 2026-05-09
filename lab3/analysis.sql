-- Лабораторная работа №3
-- Совместный анализ данных с Overture Maps и построение тематической карты
-- bbox: lon 49.2750–49.2800, lat 53.5920–53.5955 (район правок ЛР1)

INSTALL spatial;
LOAD spatial;
INSTALL httpfs;
LOAD httpfs;

SET s3_region = 'us-west-2';

-- ============================================================
-- 3.1 Загрузка пользовательского GeoJSON (результат ЛР1)
-- ============================================================

CREATE OR REPLACE TABLE user_buildings AS
SELECT
    geom,
    id                    AS osm_id,
    building,
    "building:levels"     AS building_levels,
    "addr:street"         AS addr_street,
    "addr:housenumber"    AS addr_housenumber,
    "addr:place"          AS addr_place
FROM ST_Read('../lab1/map.geojson');

SELECT
    'user_buildings' AS table_name,
    COUNT(*)         AS total_features
FROM user_buildings;

-- ============================================================
-- 3.2 Подключение данных Overture Maps (GeoParquet по S3)
-- Используется glob по всем партициям релиза с bbox-фильтром.
-- Для ускорения рекомендуется заранее определить нужные партиции
-- через run_analysis.py и указать их список явно.
-- ============================================================

CREATE OR REPLACE TABLE overture_buildings AS
SELECT
    id,
    geometry                AS geom,
    sources,
    height,
    num_floors,
    class,
    names."primary"         AS name
FROM read_parquet(
    's3://overturemaps-us-west-2/release/2026-04-15.0/theme=buildings/type=building/*.zstd.parquet'
)
WHERE
    bbox.xmin <= 49.2800
    AND bbox.xmax >= 49.2750
    AND bbox.ymin <= 53.5955
    AND bbox.ymax >= 53.5920;

SELECT
    'overture_buildings' AS table_name,
    COUNT(*)             AS total_features
FROM overture_buildings;

-- ============================================================
-- 3.3 Определение source_type через пространственное пересечение
-- my  — здание пересекается с пользовательскими данными из ЛР1
-- osm — источник Overture Maps содержит OpenStreetMap
-- ml  — прочие источники (Microsoft, Google, ML-пайплайны)
-- ============================================================

ALTER TABLE overture_buildings ADD COLUMN IF NOT EXISTS source_type VARCHAR;

CREATE OR REPLACE TEMP TABLE my_ids AS
SELECT DISTINCT o.id
FROM overture_buildings o
JOIN user_buildings u
  ON ST_Intersects(ST_SetCRS(o.geom, 'EPSG:4326'), u.geom);

UPDATE overture_buildings
SET source_type = CASE
    WHEN id IN (SELECT id FROM my_ids) THEN 'my'
    WHEN (SELECT bool_or(s.dataset ILIKE '%openstreetmap%')
          FROM unnest(sources) AS t(s)) THEN 'osm'
    ELSE 'ml'
END;

SELECT
    source_type,
    COUNT(*)                                                        AS cnt,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)             AS pct
FROM overture_buildings
GROUP BY source_type
ORDER BY cnt DESC;

-- ============================================================
-- 3.4 Экспорт итогового GeoJSON
-- Результат записывается построчно (NDJSON); для формирования
-- корректного FeatureCollection используется run_analysis.py.
-- ============================================================

COPY (
    SELECT
        ST_AsGeoJSON(geom)::JSON                      AS geometry,
        id,
        source_type,
        COALESCE(CAST(height     AS VARCHAR), '')      AS height,
        COALESCE(CAST(num_floors AS VARCHAR), '')      AS num_floors,
        COALESCE(class, '')                            AS class,
        COALESCE(name,  '')                            AS name
    FROM overture_buildings
    WHERE geom IS NOT NULL
) TO 'overture_raw.ndjson'
WITH (FORMAT JSON);
