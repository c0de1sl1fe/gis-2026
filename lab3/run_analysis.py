#!/usr/bin/env python3
"""
ЛР3: Генерация overture.geojson из данных Overture Maps + ЛР1.
Запуск: python lab3/run_analysis.py
"""
import json
import os
import sys
import duckdb
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
GEOJSON_IN    = os.path.join(BASE_DIR, '..', 'lab1', 'map-first-try.geojson')
GEOJSON_OUT   = os.path.join(BASE_DIR, 'overture.geojson')
CLIENT_PUBLIC = os.path.join(BASE_DIR, 'client', 'public', 'overture.geojson')

RELEASE = '2026-04-15.0'
S3_BASE = f's3://overturemaps-us-west-2/release/{RELEASE}/theme=buildings/type=building'

# bbox по данным changeset'а ЛР1 (с небольшим буфером ~100 м)
MINX, MINY, MAXX, MAXY = 49.2750, 53.5920, 49.2800, 53.5955


def new_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(':memory:')
    for ext in ('spatial', 'httpfs'):
        try:
            con.execute(f'LOAD {ext};')
        except Exception:
            con.execute(f'INSTALL {ext}; LOAD {ext};')
    con.execute("SET s3_region='us-west-2';")
    return con


def find_partitions(con: duckdb.DuckDBPyConnection) -> list[str]:
    print('[3.2] Listing partitions via S3 glob…')
    urls = [r[0] for r in con.execute(
        f"SELECT file FROM glob('{S3_BASE}/*.zstd.parquet')"
    ).fetchall()]
    print(f'[3.2] Total partitions in release: {len(urls)}')

    def _check(url: str) -> str | None:
        c = duckdb.connect(':memory:')
        c.execute('LOAD httpfs;')
        c.execute("SET s3_region='us-west-2';")
        try:
            row = c.execute(f"""
                SELECT
                    SUM(CASE WHEN path_in_schema = 'bbox, xmin'
                        AND TRY_CAST(stats_min_value AS DOUBLE) <= {MAXX}
                        AND TRY_CAST(stats_max_value AS DOUBLE) >= {MINX}
                        THEN 1 ELSE 0 END),
                    SUM(CASE WHEN path_in_schema = 'bbox, ymin'
                        AND TRY_CAST(stats_min_value AS DOUBLE) <= {MAXY}
                        AND TRY_CAST(stats_max_value AS DOUBLE) >= {MINY}
                        THEN 1 ELSE 0 END)
                FROM parquet_metadata('{url}')
                WHERE path_in_schema IN ('bbox, xmin','bbox, ymin')
            """).fetchone()
            if row and row[0] and row[1] and int(row[0]) > 0 and int(row[1]) > 0:
                return url
        except Exception:
            pass
        finally:
            c.close()
        return None

    print('[3.2] Scanning partition metadata in parallel…')
    matching = []
    with ThreadPoolExecutor(max_workers=30) as pool:
        for result in as_completed({pool.submit(_check, u): u for u in urls}):
            val = result.result()
            if val:
                matching.append(val)
                print(f'[3.2] Intersecting partition: {os.path.basename(val)}')

    print(f'[3.2] Intersecting partitions found: {len(matching)}')
    return matching


def load_user_buildings(con: duckdb.DuckDBPyConnection) -> None:
    path = GEOJSON_IN.replace('\\', '/')
    con.execute(f"""
        CREATE OR REPLACE TABLE user_buildings AS
        SELECT
            geom,
            id                  AS osm_id,
            building,
            "building:levels"   AS building_levels,
            "addr:street"       AS addr_street,
            "addr:housenumber"  AS addr_housenumber
        FROM ST_Read('{path}')
        WHERE ST_GeometryType(geom) IN ('POLYGON', 'MULTIPOLYGON')
          AND building IS NOT NULL AND building != ''
    """)
    n = con.execute('SELECT COUNT(*) FROM user_buildings').fetchone()[0]
    print(f'[3.1] User buildings loaded: {n}')


def load_overture_buildings(con: duckdb.DuckDBPyConnection,
                            parquet_urls: list[str]) -> int:
    url_list = ', '.join(f"'{u}'" for u in parquet_urls)
    con.execute(f"""
        CREATE OR REPLACE TABLE overture_buildings AS
        SELECT
            id,
            geometry                AS geom,
            sources,
            height,
            num_floors,
            class,
            names."primary"         AS name
        FROM read_parquet([{url_list}])
        WHERE
            bbox.xmin <= {MAXX}
            AND bbox.xmax >= {MINX}
            AND bbox.ymin <= {MAXY}
            AND bbox.ymax >= {MINY}
    """)
    n = con.execute('SELECT COUNT(*) FROM overture_buildings').fetchone()[0]
    print(f'[3.2] Overture buildings in bbox: {n}')
    return n


def classify_sources(con: duckdb.DuckDBPyConnection) -> None:
    con.execute('ALTER TABLE overture_buildings ADD COLUMN IF NOT EXISTS source_type VARCHAR;')
    con.execute("""
        CREATE OR REPLACE TEMP TABLE my_ids AS
        SELECT DISTINCT o.id
        FROM overture_buildings o
        JOIN user_buildings u
          ON try(ST_Intersects(ST_SetCRS(o.geom, 'EPSG:4326'), u.geom)) = true
    """)
    con.execute("""
        UPDATE overture_buildings
        SET source_type = CASE
            WHEN id IN (SELECT id FROM my_ids) THEN 'my'
            WHEN (SELECT bool_or(s.dataset ILIKE '%openstreetmap%')
                  FROM unnest(sources) AS t(s)) THEN 'osm'
            ELSE 'ml'
        END
    """)
    stats = con.execute(
        'SELECT source_type, COUNT(*) FROM overture_buildings '
        'GROUP BY source_type ORDER BY 2 DESC'
    ).fetchall()
    print(f'[3.3] Source stats: {stats}')


def export_geojson(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute("""
        SELECT
            ST_AsGeoJSON(geom),
            id, source_type,
            COALESCE(CAST(height     AS VARCHAR), ''),
            COALESCE(CAST(num_floors AS VARCHAR), ''),
            COALESCE(class, ''),
            COALESCE(name, '')
        FROM overture_buildings WHERE geom IS NOT NULL
    """).fetchall()

    features = [
        {
            'type': 'Feature',
            'geometry': json.loads(geom),
            'properties': {
                'id': id_, 'source_type': src,
                'height': h, 'num_floors': fl,
                'class': cls, 'name': name,
            },
        }
        for geom, id_, src, h, fl, cls, name in rows
    ]
    fc = {'type': 'FeatureCollection', 'features': features}
    for path in (GEOJSON_OUT, CLIENT_PUBLIC):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(fc, fh, ensure_ascii=False, indent=2)
    print(f'[3.4] Written {len(features)} features → overture.geojson')


def fallback_from_user(con: duckdb.DuckDBPyConnection) -> None:
    print('[WARN] Falling back to user buildings only.')
    con.execute("""
        CREATE OR REPLACE TABLE overture_buildings AS
        SELECT
            osm_id          AS id,
            geom,
            NULL::VARCHAR   AS sources,
            NULL::DOUBLE    AS height,
            NULL::INTEGER   AS num_floors,
            'building'      AS class,
            NULL::VARCHAR   AS name,
            'my'            AS source_type
        FROM user_buildings
    """)


def main() -> None:
    if not os.path.exists(GEOJSON_IN):
        print(f'ERROR: {GEOJSON_IN} not found', file=sys.stderr)
        sys.exit(1)

    con = new_con()
    load_user_buildings(con)

    try:
        partitions = find_partitions(con)
        if not partitions:
            raise RuntimeError('No intersecting partitions found.')
        load_overture_buildings(con, partitions)
        classify_sources(con)
    except Exception as exc:
        print(f'[WARN] {exc}', file=sys.stderr)
        fallback_from_user(con)

    export_geojson(con)
    con.close()
    print('Done.')


if __name__ == '__main__':
    main()
