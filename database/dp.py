"""
database/db.py — Centralized database access for BIM Component Stripper.

All PostgreSQL and Neo4j connections go through this module.
Uses connection pooling for PostgreSQL and context managers everywhere
to prevent connection leaks.
"""

import os
import logging
import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PostgreSQL connection pool
# ---------------------------------------------------------------------------

_pg_pool = None


def _get_pool():
    """Lazy-initialize the PostgreSQL connection pool."""
    global _pg_pool
    if _pg_pool is None:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", 5432),
            dbname=os.getenv("DB_NAME", "bim_components"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD"),
        )
    return _pg_pool


def get_db():
    """
    Get a PostgreSQL connection from the pool.
    Prefer using get_db_connection() context manager instead.
    If you use this directly, you MUST call release_db(conn) when done.
    """
    return _get_pool().getconn()


def release_db(conn):
    """Return a connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


@contextmanager
def get_db_connection(cursor_factory=None):
    """
    Context manager for PostgreSQL connections. Automatically commits
    on success, rolls back on error, and always returns the connection
    to the pool.

    Usage:
        with get_db_connection() as (conn, cursor):
            cursor.execute("SELECT ...")

        with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
            cursor.execute("SELECT ...")
    """
    conn = get_db()
    try:
        if cursor_factory:
            cursor = conn.cursor(cursor_factory=cursor_factory)
        else:
            cursor = conn.cursor()
        yield conn, cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        release_db(conn)


# ---------------------------------------------------------------------------
# Neo4j driver (singleton)
# ---------------------------------------------------------------------------

_neo4j_driver = None


def get_neo4j():
    """Get the Neo4j driver (singleton — reuse across calls)."""
    global _neo4j_driver
    if _neo4j_driver is None:
        _neo4j_driver = GraphDatabase.driver(
            os.getenv("NEO4J_URI", "bolt://localhost:7687"),
            auth=(
                os.getenv("NEO4J_USER", "neo4j"),
                os.getenv("NEO4J_PASSWORD"),
            ),
        )
    return _neo4j_driver


def close_neo4j():
    """Close the Neo4j driver (call on shutdown)."""
    global _neo4j_driver
    if _neo4j_driver:
        _neo4j_driver.close()
        _neo4j_driver = None


def close_pool():
    """Close the PostgreSQL connection pool (call on shutdown)."""
    global _pg_pool
    if _pg_pool:
        _pg_pool.closeall()
        _pg_pool = None


# ---------------------------------------------------------------------------
# Convenience query functions
# ---------------------------------------------------------------------------

def get_all_projects():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT * FROM projects ORDER BY uploaded_at DESC")
        return cursor.fetchall()


def get_components_by_project(project_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM components
            WHERE project_id = %s
            ORDER BY category
        """, (project_id,))
        return cursor.fetchall()


def get_components_by_category(category):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM components
            WHERE category = %s
            ORDER BY quality_score DESC
        """, (category,))
        return cursor.fetchall()


def get_high_quality_components(min_score=0.8):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM components
            WHERE quality_score >= %s
            ORDER BY quality_score DESC
        """, (min_score,))
        return cursor.fetchall()


def get_walls_by_min_length(min_length_mm):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM components
            WHERE category IN ('IfcWall', 'IfcWallStandardCase', 'IfcWallElementedCase')
            AND length_mm >= %s
            ORDER BY length_mm DESC
        """, (min_length_mm,))
        return cursor.fetchall()


def get_exterior_walls():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM components
            WHERE category IN ('IfcWall', 'IfcWallStandardCase', 'IfcWallElementedCase')
            AND parameters->'Pset_WallCommon'->>'IsExternal' = 'true'
            ORDER BY length_mm DESC
        """)
        return cursor.fetchall()


def get_spatial_data(component_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT * FROM spatial_data WHERE component_id = %s", (component_id,))
        return cursor.fetchone()


def get_components_with_spatial(project_id=None):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        if project_id:
            cursor.execute("""
                SELECT c.*, s.pos_x, s.pos_y, s.pos_z,
                       s.rot_x, s.rot_y, s.rot_z,
                       s.bounding_box, s.level, s.elevation
                FROM components c
                JOIN spatial_data s ON s.component_id = c.id
                WHERE c.project_id = %s
                ORDER BY c.category
            """, (project_id,))
        else:
            cursor.execute("""
                SELECT c.*, s.pos_x, s.pos_y, s.pos_z,
                       s.rot_x, s.rot_y, s.rot_z,
                       s.bounding_box, s.level, s.elevation
                FROM components c
                JOIN spatial_data s ON s.component_id = c.id
                ORDER BY c.category
            """)
        return cursor.fetchall()


def get_relationships_by_project(project_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT r.*,
                   c1.family_name as from_name, c1.category as from_category,
                   c2.family_name as to_name,   c2.category as to_category
            FROM relationships r
            JOIN components c1 ON c1.id = r.component_a_id
            JOIN components c2 ON c2.id = r.component_b_id
            WHERE r.project_id = %s
            ORDER BY r.relationship_type
        """, (project_id,))
        return cursor.fetchall()


def get_relationships_by_type(project_id, relationship_type):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT r.*,
                   c1.family_name as from_name, c1.category as from_category,
                   c2.family_name as to_name,   c2.category as to_category
            FROM relationships r
            JOIN components c1 ON c1.id = r.component_a_id
            JOIN components c2 ON c2.id = r.component_b_id
            WHERE r.project_id = %s AND r.relationship_type = %s
            ORDER BY r.created_at
        """, (project_id, relationship_type))
        return cursor.fetchall()


def get_component_relationships(component_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT r.*,
                   c1.family_name as from_name, c1.category as from_category,
                   c2.family_name as to_name,   c2.category as to_category
            FROM relationships r
            JOIN components c1 ON c1.id = r.component_a_id
            JOIN components c2 ON c2.id = r.component_b_id
            WHERE r.component_a_id = %s OR r.component_b_id = %s
            ORDER BY r.relationship_type
        """, (component_id, component_id))
        return cursor.fetchall()


def get_spaces_by_project(project_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM spaces WHERE project_id = %s ORDER BY level, name
        """, (project_id,))
        return cursor.fetchall()


def get_materials_by_project(project_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM materials WHERE project_id = %s
        """, (project_id,))
        return cursor.fetchall()


def get_wall_types():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT c.family_name, c.type_name, c.quality_score,
                   w.total_thickness, w.function, w.layers
            FROM wall_types w
            JOIN components c ON c.id = w.component_id
            ORDER BY w.total_thickness DESC
        """)
        return cursor.fetchall()


def get_mep_systems():
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT c.family_name, c.category, c.quality_score,
                   m.system_type, m.system_name, m.flow_rate, m.pressure_drop, m.connectors
            FROM mep_systems m
            JOIN components c ON c.id = m.component_id
            ORDER BY m.system_type
        """)
        return cursor.fetchall()


def get_reconstruction_data(project_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT c.id, c.category, c.family_name, c.type_name, c.parameters,
                   c.width_mm, c.height_mm, c.length_mm, c.area_m2, c.volume_m3,
                   s.pos_x, s.pos_y, s.pos_z, s.rot_x, s.rot_y, s.rot_z,
                   s.bounding_box, s.level, s.elevation
            FROM components c
            LEFT JOIN spatial_data s ON s.component_id = c.id
            WHERE c.project_id = %s
            ORDER BY s.pos_z, c.category
        """, (project_id,))
        return cursor.fetchall()


def search_components(search_term):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT * FROM components
            WHERE family_name ILIKE %s OR type_name ILIKE %s
            ORDER BY quality_score DESC
        """, (f'%{search_term}%', f'%{search_term}%'))
        return cursor.fetchall()


def get_component_by_revit_id(revit_id):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("SELECT * FROM components WHERE revit_id = %s", (revit_id,))
        return cursor.fetchone()


def get_components_by_level(project_id, level):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT c.*, s.pos_x, s.pos_y, s.pos_z, s.level
            FROM components c
            JOIN spatial_data s ON s.component_id = c.id
            WHERE c.project_id = %s AND s.level = %s
            ORDER BY c.category
        """, (project_id, level))
        return cursor.fetchall()


def get_mep_by_system_type(project_id, system_type):
    with get_db_connection(cursor_factory=psycopg2.extras.RealDictCursor) as (conn, cursor):
        cursor.execute("""
            SELECT c.*, m.system_type, m.system_name, m.flow_rate, m.connectors
            FROM components c
            JOIN mep_systems m ON m.component_id = c.id
            WHERE c.project_id = %s AND m.system_type = %s
        """, (project_id, system_type))
        return cursor.fetchall()
