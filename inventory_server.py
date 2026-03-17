#!/usr/bin/env python3
import json
import os
import random
import re
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # noqa: BLE001
    psycopg = None
    dict_row = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "historia_inventory.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_ENGINE = os.environ.get("DB_ENGINE", "postgres" if DATABASE_URL else "sqlite").strip().lower()
USING_POSTGRES = DB_ENGINE == "postgres"
HOST = os.environ.get("INVENTORY_HOST", "0.0.0.0")
PORT = int(os.environ.get("INVENTORY_PORT", "4100"))
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
DEFAULT_PRICING_RULES = {
    "menu_decouverte": 30.0,
    "menu_signature": 36.0,
    "single_entree": 11.0,
    "single_main": 24.0,
    "single_dessert": 8.0,
}


class DBConnection:
    def __init__(self, raw_conn, postgres=False):
        self.raw_conn = raw_conn
        self.postgres = postgres

    def _adapt_sql(self, query):
        if self.postgres:
            return query.replace("?", "%s")
        return query

    def execute(self, query, params=None):
        params = () if params is None else params
        if self.postgres:
            return self.raw_conn.execute(self._adapt_sql(query), params)
        return self.raw_conn.execute(query, params)

    def executemany(self, query, rows):
        if self.postgres:
            with self.raw_conn.cursor() as cursor:
                cursor.executemany(self._adapt_sql(query), rows)
                return cursor
        return self.raw_conn.executemany(query, rows)

    def begin(self):
        if self.postgres:
            self.raw_conn.execute("BEGIN")
        else:
            self.raw_conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.raw_conn.commit()

    def rollback(self):
        self.raw_conn.rollback()

    def close(self):
        self.raw_conn.close()

    def cursor(self):
        return DBCursor(self.raw_conn.cursor(), postgres=self.postgres)


class DBCursor:
    def __init__(self, raw_cursor, postgres=False):
        self.raw_cursor = raw_cursor
        self.postgres = postgres

    def _adapt_sql(self, query):
        if self.postgres:
            return query.replace("?", "%s")
        return query

    def execute(self, query, params=None):
        params = () if params is None else params
        return self.raw_cursor.execute(self._adapt_sql(query), params)

    def executemany(self, query, rows):
        return self.raw_cursor.executemany(self._adapt_sql(query), rows)

    def fetchone(self):
        return self.raw_cursor.fetchone()

    def fetchall(self):
        return self.raw_cursor.fetchall()

    def __getattr__(self, attr):
        return getattr(self.raw_cursor, attr)


def get_conn():
    if USING_POSTGRES:
        if not DATABASE_URL:
            raise RuntimeError("DB_ENGINE=postgres mais DATABASE_URL est vide")
        if psycopg is None:
            raise RuntimeError(
                "Pilote PostgreSQL manquant. Installe 'psycopg' puis relance."
            )
        conn = psycopg.connect(
            DATABASE_URL,
            autocommit=False,
            row_factory=dict_row,
        )
        return DBConnection(conn, postgres=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return DBConnection(conn, postgres=False)


def ensure_dish_template_course_column(conn):
    columns = conn.execute("SELECT * FROM dish_templates LIMIT 0").description
    column_names = [col[0] for col in columns]
    if "course" not in column_names:
        conn.execute(
            "ALTER TABLE dish_templates ADD COLUMN course TEXT NOT NULL DEFAULT 'main'"
        )
    conn.execute(
        """
        UPDATE dish_templates
        SET course = 'main'
        WHERE course IS NULL OR TRIM(course) = ''
        """
    )


def ensure_dish_template_management_columns(conn):
    columns = conn.execute("SELECT * FROM dish_templates LIMIT 0").description
    column_names = [col[0] for col in columns]
    if "is_active" not in column_names:
        conn.execute(
            "ALTER TABLE dish_templates ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )
    if "base_price_eur" not in column_names:
        conn.execute(
            "ALTER TABLE dish_templates ADD COLUMN base_price_eur REAL NOT NULL DEFAULT 0"
        )
    if "updated_at" not in column_names:
        conn.execute(
            "ALTER TABLE dish_templates ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
        )

    conn.execute(
        """
        UPDATE dish_templates
        SET
            is_active = 1
        WHERE is_active IS NULL
        """
    )
    conn.execute(
        """
        UPDATE dish_templates
        SET
            base_price_eur = CASE
                WHEN COALESCE(course, 'main') = 'entree' THEN 11
                WHEN COALESCE(course, 'main') = 'dessert' THEN 8
                ELSE 24
            END
        WHERE base_price_eur IS NULL OR base_price_eur <= 0
        """
    )
    now_iso = datetime.utcnow().isoformat() + "Z"
    conn.execute(
        "UPDATE dish_templates SET updated_at = ? WHERE updated_at IS NULL OR TRIM(updated_at) = ''",
        (now_iso,),
    )


def ensure_pricing_rules_table(conn):
    if USING_POSTGRES:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pricing_rules (
                key TEXT PRIMARY KEY,
                amount_eur DOUBLE PRECISION NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pricing_rules (
                key TEXT PRIMARY KEY,
                amount_eur REAL NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    now_iso = datetime.utcnow().isoformat() + "Z"
    for key, amount in DEFAULT_PRICING_RULES.items():
        existing = conn.execute(
            "SELECT key FROM pricing_rules WHERE key = ?",
            (key,),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO pricing_rules (key, amount_eur, updated_at) VALUES (?, ?, ?)",
            (key, float(amount), now_iso),
        )


def init_db():
    conn = get_conn()
    try:
        if USING_POSTGRES:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingredients (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    stock DOUBLE PRECISION NOT NULL,
                    min_stock DOUBLE PRECISION NOT NULL,
                    co2_per_unit DOUBLE PRECISION NOT NULL,
                    cost_per_unit DOUBLE PRECISION NOT NULL,
                    provenance TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    diets TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dish_templates (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    name TEXT NOT NULL,
                    style TEXT NOT NULL,
                    course TEXT NOT NULL DEFAULT 'main',
                    diet TEXT NOT NULL,
                    story TEXT NOT NULL,
                    image_url TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    base_price_eur DOUBLE PRECISION NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dish_template_items (
                    id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    dish_id INTEGER NOT NULL REFERENCES dish_templates(id),
                    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
                    quantity DOUBLE PRECISION NOT NULL
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingredients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    stock REAL NOT NULL,
                    min_stock REAL NOT NULL,
                    co2_per_unit REAL NOT NULL,
                    cost_per_unit REAL NOT NULL,
                    provenance TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    diets TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dish_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    style TEXT NOT NULL,
                    course TEXT NOT NULL DEFAULT 'main',
                    diet TEXT NOT NULL,
                    story TEXT NOT NULL,
                    image_url TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    base_price_eur REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dish_template_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dish_id INTEGER NOT NULL,
                    ingredient_id INTEGER NOT NULL,
                    quantity REAL NOT NULL,
                    FOREIGN KEY (dish_id) REFERENCES dish_templates(id),
                    FOREIGN KEY (ingredient_id) REFERENCES ingredients(id)
                )
                """
            )
        ensure_dish_template_course_column(conn)
        ensure_dish_template_management_columns(conn)
        ensure_pricing_rules_table(conn)
        conn.commit()
    finally:
        conn.close()


def seed_data_if_needed():
    conn = get_conn()
    try:
        cur = conn.cursor()
        ingredient_count = cur.execute("SELECT COUNT(*) AS c FROM ingredients").fetchone()["c"]
        dish_count = cur.execute("SELECT COUNT(*) AS c FROM dish_templates").fetchone()["c"]
        if ingredient_count > 0 and dish_count > 0:
            return

        if ingredient_count == 0:
            ingredients = [
                # Proteines omnivores / poisson
                ("Poulet fermier", "protein", "g", 26000, 7000, 0.006, 0.021, "Drôme", "Ferme des Collines", "omnivore"),
                ("Boeuf charolais", "protein", "g", 19000, 6000, 0.027, 0.034, "Ardèche", "Boucherie des Remparts", "omnivore"),
                ("Canard des Dombes", "protein", "g", 14000, 4500, 0.011, 0.031, "Dombes", "Maison Brunaud", "omnivore"),
                ("Agneau de Sisteron", "protein", "g", 12500, 4000, 0.020, 0.036, "Alpes-de-Haute-Provence", "Bergeries du Soleil", "omnivore"),
                ("Truite du Vercors", "protein", "g", 18000, 5000, 0.005, 0.026, "Vercors", "Pisciculture des Cimes", "pescatarian,omnivore"),
                ("Saumon de l'Atlantique", "protein", "g", 15000, 5000, 0.007, 0.029, "Bretagne", "Poissonnerie Oceane", "pescatarian,omnivore"),
                ("Cabillaud", "protein", "g", 14500, 4500, 0.006, 0.028, "Normandie", "Maison du Port", "pescatarian,omnivore"),
                # Proteines veges
                ("Tofu français", "protein", "g", 21000, 5500, 0.002, 0.012, "Sud-Ouest", "Soja de France", "vegan,vegetarian,omnivore"),
                ("Tempeh artisanal", "protein", "g", 16000, 4500, 0.0025, 0.014, "Lyon", "Atelier Ferment", "vegan,vegetarian,omnivore"),
                ("Pois chiches", "protein", "g", 24500, 6500, 0.0011, 0.006, "Vaucluse", "Legumineuses du Sud", "vegan,vegetarian,omnivore"),
                ("Lentilles vertes", "protein", "g", 23000, 6200, 0.0010, 0.0065, "Le Puy", "Coop Lentille", "vegan,vegetarian,omnivore"),
                ("Haricots rouges", "protein", "g", 21000, 6000, 0.0012, 0.0062, "Loire", "Grainier Bio", "vegan,vegetarian,omnivore"),
                ("Oeufs plein air", "protein", "piece", 900, 220, 0.24, 0.43, "Drôme", "Ferme des Poulettes", "vegetarian,pescatarian,omnivore"),
                ("Fromage de chèvre", "protein", "g", 13000, 3600, 0.008, 0.019, "Nyons", "Fromagerie des Oliviers", "vegetarian,omnivore"),
                ("Halloumi", "protein", "g", 12000, 3200, 0.007, 0.018, "Rhône", "Laiterie du Rhône", "vegetarian,omnivore"),
                # Bases
                ("Riz de Camargue", "base", "g", 36000, 9000, 0.0014, 0.0048, "Camargue", "Rizière Delta", "vegan,vegetarian,pescatarian,omnivore"),
                ("Quinoa de la Drôme", "base", "g", 29500, 7600, 0.0018, 0.0058, "Drôme", "Coop Quinoa", "vegan,vegetarian,pescatarian,omnivore"),
                ("Polenta artisanale", "base", "g", 27000, 7000, 0.0012, 0.0046, "Isère", "Moulin des Alpes", "vegan,vegetarian,pescatarian,omnivore"),
                ("Ravioles du Royans", "base", "g", 24000, 6500, 0.0029, 0.0085, "Royans", "Maison Rambert", "vegetarian,pescatarian,omnivore"),
                ("Pommes de terre grenaille", "base", "g", 40000, 10000, 0.0009, 0.0036, "Drôme", "Maraîcher Vieux Chêne", "vegan,vegetarian,pescatarian,omnivore"),
                ("Semoule complète", "base", "g", 25500, 6400, 0.0011, 0.0042, "Occitanie", "Moulin du Sud", "vegan,vegetarian,pescatarian,omnivore"),
                ("Nouilles soba", "base", "g", 22000, 5800, 0.0016, 0.0054, "France", "Atelier Sarrasin", "vegan,vegetarian,pescatarian,omnivore"),
                ("Patate douce rôtie", "base", "g", 28000, 7000, 0.0010, 0.0045, "Espagne", "Primeur des Halles", "vegan,vegetarian,pescatarian,omnivore"),
                ("Pâtes fraîches", "base", "g", 26000, 6800, 0.0021, 0.0056, "Valence", "Pastificio Valence", "vegetarian,pescatarian,omnivore"),
                ("Blé concassé", "base", "g", 25000, 6200, 0.0010, 0.0040, "Drôme", "Moulin Roman", "vegan,vegetarian,pescatarian,omnivore"),
                # Légumes
                ("Carottes multicolores", "vegetable", "g", 32000, 8500, 0.0008, 0.0038, "Drôme", "Maraîcher Bio Valence", "vegan,vegetarian,pescatarian,omnivore"),
                ("Courgettes", "vegetable", "g", 30000, 8000, 0.0007, 0.0039, "Ardèche", "Ferme du Ventoux", "vegan,vegetarian,pescatarian,omnivore"),
                ("Poivrons rouges", "vegetable", "g", 26000, 7000, 0.0009, 0.0044, "Gard", "Primeur des Halles", "vegan,vegetarian,pescatarian,omnivore"),
                ("Champignons forestiers", "vegetable", "g", 18000, 5200, 0.0013, 0.0080, "Vercors", "Cueillette des Monts", "vegan,vegetarian,pescatarian,omnivore"),
                ("Epinards", "vegetable", "g", 24000, 6200, 0.0008, 0.0041, "Drôme", "Maraîcher Bio Valence", "vegan,vegetarian,pescatarian,omnivore"),
                ("Betteraves", "vegetable", "g", 21000, 5200, 0.0008, 0.0035, "Drôme", "Maraîcher Bio Valence", "vegan,vegetarian,pescatarian,omnivore"),
                ("Fenouil", "vegetable", "g", 19000, 5000, 0.0008, 0.0042, "PACA", "Primeur des Halles", "vegan,vegetarian,pescatarian,omnivore"),
                ("Tomates anciennes", "vegetable", "g", 28000, 7600, 0.0010, 0.0048, "Valence", "Serres des Coteaux", "vegan,vegetarian,pescatarian,omnivore"),
                ("Brocoli", "vegetable", "g", 23000, 6000, 0.0009, 0.0041, "Isère", "Ferme des Berges", "vegan,vegetarian,pescatarian,omnivore"),
                ("Chou rouge", "vegetable", "g", 22000, 5800, 0.0007, 0.0037, "Drôme", "Maraîcher Bio Valence", "vegan,vegetarian,pescatarian,omnivore"),
                # Sauces / aromates
                ("Sauce yaourt citron", "sauce", "ml", 7200, 1800, 0.0014, 0.012, "Valence", "Atelier HistorIA", "vegetarian,pescatarian,omnivore"),
                ("Sauce miso sésame", "sauce", "ml", 6500, 1600, 0.0012, 0.011, "Lyon", "Atelier Umami", "vegan,vegetarian,pescatarian,omnivore"),
                ("Sauce tomate basilic", "sauce", "ml", 7600, 1900, 0.0009, 0.008, "Drôme", "Conserverie Romaine", "vegan,vegetarian,pescatarian,omnivore"),
                ("Sauce crème truffée", "sauce", "ml", 4200, 1200, 0.0026, 0.019, "Périgord", "Maison Truffe", "vegetarian,pescatarian,omnivore"),
                ("Vinaigrette agrumes", "sauce", "ml", 6100, 1500, 0.0010, 0.009, "Nice", "Agrumeraie du Cap", "vegan,vegetarian,pescatarian,omnivore"),
                ("Sauce coco gingembre", "sauce", "ml", 5600, 1450, 0.0018, 0.010, "Bordeaux", "Atelier Exotique", "vegan,vegetarian,pescatarian,omnivore"),
                ("Sauce soja", "sauce", "ml", 7000, 1600, 0.0011, 0.007, "Nantes", "Maison Soja", "vegan,vegetarian,pescatarian,omnivore"),
                ("Ail", "herb", "g", 6500, 1400, 0.0006, 0.003, "Drôme", "Ferme des Arômes", "vegan,vegetarian,pescatarian,omnivore"),
                ("Thym", "herb", "g", 2600, 600, 0.0004, 0.012, "Provence", "Herboristerie du Soleil", "vegan,vegetarian,pescatarian,omnivore"),
                ("Basilic", "herb", "g", 2900, 700, 0.0005, 0.014, "Valence", "Herboristerie du Soleil", "vegan,vegetarian,pescatarian,omnivore"),
                ("Coriandre fraîche", "herb", "g", 2400, 600, 0.0005, 0.013, "PACA", "Herboristerie du Soleil", "vegan,vegetarian,pescatarian,omnivore"),
                ("Persil plat", "herb", "g", 2800, 700, 0.0005, 0.010, "Drôme", "Herboristerie du Soleil", "vegan,vegetarian,pescatarian,omnivore"),
                ("Noisettes concassées", "garnish", "g", 3600, 900, 0.0018, 0.020, "Ardèche", "Noiseraie Centrale", "vegan,vegetarian,pescatarian,omnivore"),
                ("Graines de courge", "garnish", "g", 4100, 1000, 0.0012, 0.016, "Loire", "Grainier Bio", "vegan,vegetarian,pescatarian,omnivore"),
                ("Citron jaune", "garnish", "piece", 480, 120, 0.09, 0.34, "Menton", "Agrumeraie du Cap", "vegan,vegetarian,pescatarian,omnivore"),
                ("Huile d'olive", "sauce", "ml", 9000, 2200, 0.0016, 0.010, "Nyons", "Moulin des Olives", "vegan,vegetarian,pescatarian,omnivore"),
                ("Miel local", "garnish", "g", 3000, 700, 0.0015, 0.018, "Drôme", "Rucher des Coteaux", "vegetarian,pescatarian,omnivore"),
            ]
            cur.executemany(
                """
                INSERT INTO ingredients (
                    name, category, unit, stock, min_stock, co2_per_unit,
                    cost_per_unit, provenance, supplier, diets
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ingredients,
            )

        if dish_count == 0:
            ingredient_rows = cur.execute("SELECT id, name FROM ingredients").fetchall()
            ingredient_ids = {row["name"]: row["id"] for row in ingredient_rows}

            style_profiles = {
                "terroir": {
                    "label": "Terroir",
                    "bases": [
                        "Ravioles du Royans",
                        "Polenta artisanale",
                        "Pommes de terre grenaille",
                        "Blé concassé",
                        "Pâtes fraîches",
                    ],
                    "veggies": ["Carottes multicolores", "Champignons forestiers", "Fenouil", "Tomates anciennes", "Brocoli"],
                    "sauces": ["Sauce crème truffée", "Sauce tomate basilic", "Sauce yaourt citron"],
                    "images": [
                        "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1547592166-23ac45744acd?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=1200&q=80",
                    ],
                    "history": "Inspiré des tables drômoises, ce plat met en avant une cuisson lente et le respect des saisons.",
                },
                "classique": {
                    "label": "Classique",
                    "bases": ["Pâtes fraîches", "Riz de Camargue", "Pommes de terre grenaille", "Semoule complète", "Polenta artisanale"],
                    "veggies": ["Carottes multicolores", "Courgettes", "Tomates anciennes", "Epinards", "Brocoli"],
                    "sauces": ["Sauce tomate basilic", "Sauce yaourt citron", "Sauce crème truffée"],
                    "images": [
                        "https://images.unsplash.com/photo-1543339494-b4cd4f7ba686?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1504674900247-0877df9cc836?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1467003909585-2f8a72700288?auto=format&fit=crop&w=1200&q=80",
                    ],
                    "history": "Version contemporaine d'une assiette familiale, avec un calibrage précis des portions pour limiter le gaspillage.",
                },
                "exotique": {
                    "label": "Exotique",
                    "bases": ["Riz de Camargue", "Quinoa de la Drôme", "Nouilles soba", "Patate douce rôtie", "Semoule complète"],
                    "veggies": ["Poivrons rouges", "Chou rouge", "Epinards", "Carottes multicolores", "Brocoli"],
                    "sauces": ["Sauce coco gingembre", "Sauce miso sésame", "Sauce soja"],
                    "images": [
                        "https://images.unsplash.com/photo-1512058564366-18510be2db19?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1511690743698-d9d85f2fbf38?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1515003197210-e0cd71810b5f?auto=format&fit=crop&w=1200&q=80",
                    ],
                    "history": "Né des influences de route des épices, ce plat marie techniques françaises et assaisonnements internationaux.",
                },
                "fraicheur": {
                    "label": "Fraîcheur",
                    "bases": ["Quinoa de la Drôme", "Blé concassé", "Semoule complète", "Riz de Camargue", "Patate douce rôtie"],
                    "veggies": ["Fenouil", "Betteraves", "Courgettes", "Epinards", "Tomates anciennes"],
                    "sauces": ["Vinaigrette agrumes", "Sauce yaourt citron", "Sauce miso sésame"],
                    "images": [
                        "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1473093295043-cdd812d0e601?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1498837167922-ddd27525d352?auto=format&fit=crop&w=1200&q=80",
                    ],
                    "history": "Pensé pour des services rapides, ce format fraîcheur favorise les circuits courts et les cuissons douces.",
                },
                "fusion": {
                    "label": "Fusion",
                    "bases": ["Nouilles soba", "Ravioles du Royans", "Polenta artisanale", "Riz de Camargue", "Pâtes fraîches"],
                    "veggies": ["Poivrons rouges", "Champignons forestiers", "Brocoli", "Chou rouge", "Epinards"],
                    "sauces": ["Sauce miso sésame", "Sauce tomate basilic", "Sauce coco gingembre"],
                    "images": [
                        "https://images.unsplash.com/photo-1482049016688-2d3e1b311543?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1466978913421-dad2ebd01d17?auto=format&fit=crop&w=1200&q=80",
                        "https://images.unsplash.com/photo-1540189549336-e6e99c3679fe?auto=format&fit=crop&w=1200&q=80",
                    ],
                    "history": "Une base locale retravaillée avec des gestes de street-food gastronomique pour une signature visuelle forte.",
                },
            }

            protein_map = {
                "omnivore": ["Poulet fermier", "Boeuf charolais", "Canard des Dombes", "Agneau de Sisteron"],
                "pescatarian": ["Truite du Vercors", "Saumon de l'Atlantique", "Cabillaud", "Oeufs plein air"],
                "vegetarian": ["Oeufs plein air", "Fromage de chèvre", "Halloumi", "Lentilles vertes"],
                "vegan": ["Tofu français", "Tempeh artisanal", "Pois chiches", "Haricots rouges"],
            }

            herbs = ["Ail", "Thym", "Basilic", "Coriandre fraîche", "Persil plat"]
            garnishes_by_diet = {
                "vegan": ["Graines de courge", "Noisettes concassées", "Citron jaune"],
                "vegetarian": ["Noisettes concassées", "Citron jaune", "Miel local"],
                "pescatarian": ["Citron jaune", "Noisettes concassées", "Graines de courge"],
                "omnivore": ["Noisettes concassées", "Graines de courge", "Miel local"],
            }

            dish_templates = []
            dish_items = []
            rng = random.Random(42)

            for diet, proteins in protein_map.items():
                for style_key, style in style_profiles.items():
                    for idx in range(14):
                        protein = proteins[idx % len(proteins)]
                        base = style["bases"][(idx + 1) % len(style["bases"])]
                        veg1 = style["veggies"][idx % len(style["veggies"])]
                        veg2 = style["veggies"][(idx + 2) % len(style["veggies"])]
                        sauce = style["sauces"][(idx + 1) % len(style["sauces"])]
                        herb = herbs[(idx + len(style_key)) % len(herbs)]
                        garnish = garnishes_by_diet[diet][idx % len(garnishes_by_diet[diet])]

                        naming_token = [
                            "Signature",
                            "Atelier",
                            "Création",
                            "Sélection",
                            "Héritage",
                            "Collection",
                            "Édition",
                        ][idx % 7]

                        dish_name = f"{style['label']} {naming_token} {protein} & {base} #{idx + 1}"
                        story = (
                            f"{style['history']} Le binôme {protein.lower()} / {base.lower()} "
                            f"est dressé avec {veg1.lower()} et {veg2.lower()} pour garder une identité {style['label'].lower()}."
                        )
                        image_url = style["images"][idx % len(style["images"])]

                        dish_templates.append((dish_name, style_key, diet, story, image_url))

                        protein_qty = 160 if diet == "omnivore" else 150 if diet == "pescatarian" else 130
                        if "Oeufs" in protein:
                            protein_qty = 2
                        garnish_qty = 1 if garnish == "Citron jaune" else 14

                        items = [
                            (protein, protein_qty),
                            (base, 140),
                            (veg1, 80),
                            (veg2, 70),
                            (sauce, 35),
                            (herb, 4),
                            (garnish, garnish_qty),
                            ("Huile d'olive", 8),
                        ]
                        dish_items.append(items)

            cur.executemany(
                """
                INSERT INTO dish_templates (name, style, diet, story, image_url)
                VALUES (?, ?, ?, ?, ?)
                """,
                dish_templates,
            )

            dish_id_rows = cur.execute("SELECT id FROM dish_templates ORDER BY id").fetchall()
            dish_id_list = [row["id"] for row in dish_id_rows]

            rows_to_insert = []
            for dish_id, item_group in zip(dish_id_list, dish_items):
                for ingredient_name, quantity in item_group:
                    ingredient_id = ingredient_ids.get(ingredient_name)
                    if ingredient_id is None:
                        continue
                    rows_to_insert.append((dish_id, ingredient_id, quantity))

            cur.executemany(
                """
                INSERT INTO dish_template_items (dish_id, ingredient_id, quantity)
                VALUES (?, ?, ?)
                """,
                rows_to_insert,
            )

        conn.commit()
    finally:
        conn.close()


def localize_dataset_to_nice(conn):
    # Force provenance and suppliers to Nice / Alpes-Maritimes ecosystem.
    conn.execute(
        """
        UPDATE ingredients
        SET
            provenance = CASE
                WHEN category = 'protein' THEN 'Nice et alentours (Alpes-Maritimes)'
                WHEN category = 'base' THEN 'Plaine du Var (Nice)'
                WHEN category = 'vegetable' THEN 'Vallée du Var (Nice)'
                WHEN category = 'sauce' THEN 'Vieux-Nice'
                WHEN category = 'herb' THEN 'Collines de Nice'
                WHEN category = 'garnish' THEN 'Menton et Nice'
                WHEN category = 'dessert' THEN 'Nice et arrière-pays'
                ELSE 'Nice et alentours (Alpes-Maritimes)'
            END,
            supplier = CASE
                WHEN category = 'protein' THEN 'Coop des Producteurs Niçois'
                WHEN category = 'base' THEN 'Moulin Riviera Nice'
                WHEN category = 'vegetable' THEN 'Maraîchers de Nice Côte d''Azur'
                WHEN category = 'sauce' THEN 'Atelier Culinaire Nice'
                WHEN category = 'herb' THEN 'Herboristerie du Vieux-Nice'
                WHEN category = 'garnish' THEN 'Marché de la Libération Nice'
                WHEN category = 'dessert' THEN 'Pâtissiers de Nice'
                ELSE 'Réseau Fournisseurs Nice'
            END
        """
    )

    ingredient_rename_map = {
        "Truite du Vercors": "Truite niçoise",
        "Quinoa de la Drôme": "Quinoa niçois",
        "Ravioles du Royans": "Ravioles niçoises",
        "Riz de Camargue": "Riz niçois",
        "Poulet fermier": "Poulet fermier niçois",
        "Boeuf charolais": "Boeuf niçois",
        "Canard des Dombes": "Canard niçois",
        "Agneau de Sisteron": "Agneau niçois",
        "Saumon de l'Atlantique": "Saumon niçois",
        "Cabillaud": "Cabillaud niçois",
        "Tofu français": "Tofu niçois",
        "Tempeh artisanal": "Tempeh niçois",
        "Pois chiches": "Pois chiches niçois",
        "Lentilles vertes": "Lentilles niçoises",
        "Haricots rouges": "Haricots rouges niçois",
        "Oeufs plein air": "Oeufs plein air niçois",
        "Fromage de chèvre": "Fromage de chèvre niçois",
        "Halloumi": "Halloumi niçois",
        "Polenta artisanale": "Polenta niçoise",
        "Pommes de terre grenaille": "Pommes de terre niçoises",
        "Semoule complète": "Semoule niçoise",
        "Nouilles soba": "Nouilles soba niçoises",
        "Patate douce rôtie": "Patate douce niçoise",
        "Pâtes fraîches": "Pâtes fraîches niçoises",
        "Blé concassé": "Blé concassé niçois",
        "Carottes multicolores": "Carottes niçoises",
        "Courgettes": "Courgettes niçoises",
        "Poivrons rouges": "Poivrons rouges niçois",
        "Champignons forestiers": "Champignons niçois",
        "Epinards": "Epinards niçois",
        "Betteraves": "Betteraves niçoises",
        "Fenouil": "Fenouil niçois",
        "Tomates anciennes": "Tomates niçoises",
        "Brocoli": "Brocoli niçois",
        "Chou rouge": "Chou rouge niçois",
        "Sauce yaourt citron": "Sauce citron niçoise",
        "Sauce miso sésame": "Sauce sésame niçoise",
        "Sauce tomate basilic": "Sauce tomate niçoise",
        "Sauce crème truffée": "Sauce crème niçoise",
        "Vinaigrette agrumes": "Vinaigrette niçoise",
        "Sauce coco gingembre": "Sauce gingembre niçoise",
        "Sauce soja": "Sauce soja niçoise",
        "Ail": "Ail niçois",
        "Thym": "Thym niçois",
        "Basilic": "Basilic niçois",
        "Coriandre fraîche": "Coriandre niçoise",
        "Persil plat": "Persil niçois",
        "Noisettes concassées": "Noisettes niçoises",
        "Graines de courge": "Graines de courge niçoises",
        "Citron jaune": "Citron de Menton",
        "Huile d'olive": "Huile d'olive niçoise",
        "Miel local": "Miel niçois",
    }

    for old_name, new_name in ingredient_rename_map.items():
        conn.execute(
            "UPDATE ingredients SET name = ? WHERE name = ?",
            (new_name, old_name),
        )

    templates = conn.execute("SELECT id, name, story FROM dish_templates").fetchall()
    if not templates:
        return

    text_replacements = {
        "drômoises": "niçoises",
        "Drôme": "Nice",
        "Valence": "Nice",
        "Ardèche": "Nice",
        "Vercors": "Nice",
        "Royans": "Nice",
        "Le Puy": "Nice",
        "Nyons": "Nice",
        "PACA": "Alpes-Maritimes",
        "Camargue": "Nice",
        "Occitanie": "Nice",
        "Bretagne": "Nice",
        "Normandie": "Nice",
        "Lyon": "Nice",
        "Vaucluse": "Nice",
        "Loire": "Nice",
        "Isère": "Nice",
        "Provence": "Nice",
        "Menton": "Menton (Alpes-Maritimes)",
        "Producteurs d'Herbes Aromatiques": "Producteurs niçois",
    }
    text_replacements.update(ingredient_rename_map)
    for source, target in ingredient_rename_map.items():
        text_replacements[source.lower()] = target.lower()

    text_replacements.update(
        {
            "Ravioles du Nice": "Ravioles niçoises",
            "ravioles du nice": "ravioles niçoises",
        }
    )

    rename_sensitive_sources = set(ingredient_rename_map.keys())
    rename_sensitive_sources.update({key.lower() for key in ingredient_rename_map.keys()})
    rename_sensitive_sources.update({"Ravioles du Nice", "ravioles du nice"})

    def apply_text_replacements(text):
        value = text
        for source, target in text_replacements.items():
            if source in rename_sensitive_sources:
                pattern = re.escape(source) + r"(?!\s+niçois(?:e|es)?)"
                value = re.sub(pattern, target, value)
            else:
                value = value.replace(source, target)

        # Safety cleanup if localization runs multiple times.
        for token in ("niçois", "niçoise", "niçoises"):
            value = re.sub(rf"\b{token}(?:\s+{token})+\b", token, value)
        value = re.sub(r"\s{2,}", " ", value).strip()
        return value

    for row in templates:
        new_name = apply_text_replacements(row["name"])
        new_story = apply_text_replacements(row["story"])

        if "Nice" not in new_story and "niçois" not in new_story and "niçoise" not in new_story:
            new_story = (
                f"{new_story} Cette création met en avant les producteurs de Nice et des Alpes-Maritimes."
            )

        conn.execute(
            "UPDATE dish_templates SET name = ?, story = ? WHERE id = ?",
            (new_name, new_story, row["id"]),
        )

    conn.commit()


def ensure_extended_ingredients(conn):
    extra_ingredients = [
        (
            "Mesclun niçois",
            "vegetable",
            "g",
            22000,
            5000,
            0.0006,
            0.0038,
            "Vallée du Var (Nice)",
            "Maraîchers de Nice Côte d'Azur",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Artichaut violet niçois",
            "vegetable",
            "g",
            17000,
            4200,
            0.0009,
            0.0049,
            "Nice et alentours (Alpes-Maritimes)",
            "Maraîchers de Nice Côte d'Azur",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Radis niçois",
            "vegetable",
            "g",
            14000,
            3600,
            0.0005,
            0.0032,
            "Nice et alentours (Alpes-Maritimes)",
            "Maraîchers de Nice Côte d'Azur",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Anchois niçois",
            "protein",
            "g",
            9000,
            2500,
            0.0042,
            0.022,
            "Port de Nice",
            "Pêcheurs de Nice",
            "pescatarian,omnivore",
        ),
        (
            "Socca niçoise",
            "base",
            "g",
            16000,
            4000,
            0.0012,
            0.0085,
            "Vieux-Nice",
            "Atelier Culinaire Nice",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Tapenade niçoise",
            "sauce",
            "g",
            7600,
            1800,
            0.0013,
            0.0115,
            "Vieux-Nice",
            "Atelier Culinaire Nice",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Fraises niçoises",
            "dessert",
            "g",
            13000,
            3200,
            0.0008,
            0.0068,
            "Plaine du Var (Nice)",
            "Maraîchers de Nice Côte d'Azur",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Pêches niçoises",
            "dessert",
            "g",
            12500,
            3000,
            0.0008,
            0.0065,
            "Collines de Nice",
            "Maraîchers de Nice Côte d'Azur",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Abricots niçois",
            "dessert",
            "g",
            11800,
            2800,
            0.0009,
            0.0062,
            "Collines de Nice",
            "Maraîchers de Nice Côte d'Azur",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Amandes niçoises",
            "dessert",
            "g",
            5200,
            1200,
            0.0016,
            0.017,
            "Arrière-pays niçois",
            "Producteurs des Alpes-Maritimes",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Yaourt fermier niçois",
            "dessert",
            "g",
            8800,
            2200,
            0.0017,
            0.012,
            "Nice et alentours (Alpes-Maritimes)",
            "Coop des Producteurs Niçois",
            "vegetarian,pescatarian,omnivore",
        ),
        (
            "Mascarpone niçois",
            "dessert",
            "g",
            7200,
            1700,
            0.0024,
            0.015,
            "Nice et alentours (Alpes-Maritimes)",
            "Coop des Producteurs Niçois",
            "vegetarian,pescatarian,omnivore",
        ),
        (
            "Biscuit croustillant niçois",
            "dessert",
            "g",
            6800,
            1600,
            0.0015,
            0.0105,
            "Vieux-Nice",
            "Atelier Culinaire Nice",
            "vegetarian,pescatarian,omnivore",
        ),
        (
            "Sorbet citron niçois",
            "dessert",
            "g",
            7600,
            1800,
            0.0011,
            0.0095,
            "Menton et Nice",
            "Agrumeraie du Cap",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Chocolat noir niçois",
            "dessert",
            "g",
            6200,
            1500,
            0.0022,
            0.0145,
            "Nice et alentours (Alpes-Maritimes)",
            "Atelier Chocolat Nice",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
        (
            "Coulis fruits rouges niçois",
            "dessert",
            "ml",
            6400,
            1500,
            0.0008,
            0.0089,
            "Plaine du Var (Nice)",
            "Atelier Culinaire Nice",
            "vegan,vegetarian,pescatarian,omnivore",
        ),
    ]

    existing_names = {
        row["name"] for row in conn.execute("SELECT name FROM ingredients").fetchall()
    }
    rows_to_insert = [row for row in extra_ingredients if row[0] not in existing_names]
    if not rows_to_insert:
        return

    conn.executemany(
        """
        INSERT INTO ingredients (
            name, category, unit, stock, min_stock, co2_per_unit,
            cost_per_unit, provenance, supplier, diets
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )


def quantity_for_ingredient(ingredient_name):
    if ingredient_name in {"Oeufs plein air niçois", "Citron de Menton"}:
        return 1
    if ingredient_name == "Socca niçoise":
        return 95
    if ingredient_name in {"Tapenade niçoise", "Vinaigrette niçoise", "Sauce citron niçoise"}:
        return 18
    if ingredient_name in {"Yaourt fermier niçois", "Mascarpone niçois", "Sorbet citron niçois"}:
        return 70
    if ingredient_name in {"Biscuit croustillant niçois", "Chocolat noir niçois"}:
        return 28
    if ingredient_name in {"Coulis fruits rouges niçois"}:
        return 22
    if ingredient_name in {"Amandes niçoises", "Noisettes niçoises"}:
        return 10
    return 75


def ensure_menu_course_templates(conn):
    ensure_dish_template_course_column(conn)
    course_counts = {
        row["course"]: row["c"]
        for row in conn.execute(
            "SELECT course, COUNT(*) AS c FROM dish_templates GROUP BY course"
        ).fetchall()
    }
    if course_counts.get("entree", 0) >= 100 and course_counts.get("dessert", 0) >= 100:
        return

    ingredient_ids = {
        row["name"]: row["id"]
        for row in conn.execute("SELECT id, name FROM ingredients").fetchall()
    }

    entree_styles = {
        "terroir": {
            "label": "Terroir",
            "veg": [
                "Mesclun niçois",
                "Tomates niçoises",
                "Fenouil niçois",
                "Artichaut violet niçois",
                "Radis niçois",
            ],
            "sauces": ["Vinaigrette niçoise", "Tapenade niçoise", "Huile d'olive niçoise"],
            "images": [
                "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1498837167922-ddd27525d352?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "classique": {
            "label": "Classique",
            "veg": [
                "Tomates niçoises",
                "Courgettes niçoises",
                "Mesclun niçois",
                "Radis niçois",
                "Fenouil niçois",
            ],
            "sauces": ["Sauce citron niçoise", "Vinaigrette niçoise", "Huile d'olive niçoise"],
            "images": [
                "https://images.unsplash.com/photo-1540189549336-e6e99c3679fe?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1490645935967-10de6ba17061?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "fraicheur": {
            "label": "Fraîcheur",
            "veg": [
                "Mesclun niçois",
                "Courgettes niçoises",
                "Tomates niçoises",
                "Radis niçois",
                "Fenouil niçois",
            ],
            "sauces": ["Vinaigrette niçoise", "Sauce citron niçoise", "Huile d'olive niçoise"],
            "images": [
                "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1473093295043-cdd812d0e601?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "exotique": {
            "label": "Exotique",
            "veg": [
                "Poivrons rouges niçois",
                "Mesclun niçois",
                "Chou rouge niçois",
                "Tomates niçoises",
                "Fenouil niçois",
            ],
            "sauces": ["Sauce gingembre niçoise", "Sauce sésame niçoise", "Sauce soja niçoise"],
            "images": [
                "https://images.unsplash.com/photo-1512058564366-18510be2db19?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1511690743698-d9d85f2fbf38?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "fusion": {
            "label": "Fusion",
            "veg": [
                "Tomates niçoises",
                "Courgettes niçoises",
                "Mesclun niçois",
                "Poivrons rouges niçois",
                "Radis niçois",
            ],
            "sauces": ["Tapenade niçoise", "Sauce sésame niçoise", "Vinaigrette niçoise"],
            "images": [
                "https://images.unsplash.com/photo-1466978913421-dad2ebd01d17?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1482049016688-2d3e1b311543?auto=format&fit=crop&w=1200&q=80",
            ],
        },
    }

    entree_leads = {
        "omnivore": ["Poulet fermier niçois", "Anchois niçois", "Boeuf niçois"],
        "pescatarian": ["Anchois niçois", "Truite niçoise", "Saumon niçois"],
        "vegetarian": ["Oeufs plein air niçois", "Fromage de chèvre niçois", "Halloumi niçois"],
        "vegan": ["Tofu niçois", "Pois chiches niçois", "Socca niçoise"],
    }

    dessert_styles = {
        "terroir": {
            "label": "Terroir",
            "fruits": ["Abricots niçois", "Pêches niçoises", "Fraises niçoises"],
            "images": [
                "https://images.unsplash.com/photo-1499636136210-6f4ee915583e?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1464305795204-6f5bbfc7fb81?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "classique": {
            "label": "Classique",
            "fruits": ["Fraises niçoises", "Abricots niçois", "Pêches niçoises"],
            "images": [
                "https://images.unsplash.com/photo-1551024601-bec78aea704b?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1488477181946-6428a0291777?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "fraicheur": {
            "label": "Fraîcheur",
            "fruits": ["Fraises niçoises", "Pêches niçoises", "Abricots niçois"],
            "images": [
                "https://images.unsplash.com/photo-1505253758473-96b7015fcd40?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1432139555190-58524dae6a55?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "exotique": {
            "label": "Exotique",
            "fruits": ["Pêches niçoises", "Abricots niçois", "Fraises niçoises"],
            "images": [
                "https://images.unsplash.com/photo-1464349095431-e9a21285b5f3?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1461009209120-103d8f0b4f6f?auto=format&fit=crop&w=1200&q=80",
            ],
        },
        "fusion": {
            "label": "Fusion",
            "fruits": ["Fraises niçoises", "Abricots niçois", "Pêches niçoises"],
            "images": [
                "https://images.unsplash.com/photo-1550617931-e17a7b70dce2?auto=format&fit=crop&w=1200&q=80",
                "https://images.unsplash.com/photo-1467003909585-2f8a72700288?auto=format&fit=crop&w=1200&q=80",
            ],
        },
    }

    dessert_cores = {
        "omnivore": ["Yaourt fermier niçois", "Mascarpone niçois", "Sorbet citron niçois"],
        "pescatarian": ["Yaourt fermier niçois", "Mascarpone niçois", "Sorbet citron niçois"],
        "vegetarian": ["Yaourt fermier niçois", "Mascarpone niçois", "Sorbet citron niçois"],
        "vegan": ["Sorbet citron niçois", "Chocolat noir niçois", "Coulis fruits rouges niçois"],
    }

    dish_templates = []
    dish_items = []

    for diet, leads in entree_leads.items():
        for style_key, style in entree_styles.items():
            for idx in range(6):
                lead = leads[idx % len(leads)]
                veg1 = style["veg"][idx % len(style["veg"])]
                veg2 = style["veg"][(idx + 2) % len(style["veg"])]
                sauce = style["sauces"][idx % len(style["sauces"])]
                garnish = "Citron de Menton" if idx % 2 == 0 else "Amandes niçoises"
                base = "Socca niçoise" if idx % 3 == 0 else "Mesclun niçois"

                dish_name = f"Entrée {style['label']} {diet} Nice #{idx + 1}"
                story = (
                    "Entrée pensée pour un service niçois: fraîcheur, produits locaux et assaisonnement précis, "
                    "avec un montage rapide en cuisine."
                )
                image_url = style["images"][idx % len(style["images"])]

                dish_templates.append((dish_name, style_key, "entree", diet, story, image_url))
                items = [
                    (lead, quantity_for_ingredient(lead)),
                    (base, quantity_for_ingredient(base)),
                    (veg1, quantity_for_ingredient(veg1) - 10),
                    (veg2, quantity_for_ingredient(veg2) - 15),
                    (sauce, quantity_for_ingredient(sauce)),
                    (garnish, quantity_for_ingredient(garnish)),
                    ("Huile d'olive niçoise", 7),
                ]
                dish_items.append(items)

    for diet, cores in dessert_cores.items():
        for style_key, style in dessert_styles.items():
            for idx in range(6):
                core = cores[idx % len(cores)]
                fruit1 = style["fruits"][idx % len(style["fruits"])]
                fruit2 = style["fruits"][(idx + 1) % len(style["fruits"])]
                crunch = "Biscuit croustillant niçois" if diet != "vegan" else "Amandes niçoises"
                topping = "Miel niçois" if (diet != "vegan" and idx % 2 == 0) else "Coulis fruits rouges niçois"

                dish_name = f"Dessert {style['label']} {diet} Nice #{idx + 1}"
                story = (
                    "Dessert calibré pour la carte niçoise: fruit, texture et équilibre sucré, "
                    "pensé pour une finition régulière au passe."
                )
                image_url = style["images"][idx % len(style["images"])]

                dish_templates.append((dish_name, style_key, "dessert", diet, story, image_url))
                items = [
                    (core, quantity_for_ingredient(core)),
                    (fruit1, 55),
                    (fruit2, 45),
                    (crunch, quantity_for_ingredient(crunch)),
                    (topping, quantity_for_ingredient(topping)),
                    ("Citron de Menton", 1 if idx % 3 == 0 else 0.5),
                ]
                dish_items.append(items)

    existing_names = {
        row["name"] for row in conn.execute("SELECT name FROM dish_templates").fetchall()
    }
    filtered_templates = []
    filtered_items = []
    for template_row, items in zip(dish_templates, dish_items):
        if template_row[0] in existing_names:
            continue
        filtered_templates.append(template_row)
        filtered_items.append(items)

    if not filtered_templates:
        return

    conn.executemany(
        """
        INSERT INTO dish_templates (name, style, course, diet, story, image_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        filtered_templates,
    )

    inserted_rows = conn.execute(
        """
        SELECT id, name
        FROM dish_templates
        WHERE name LIKE 'Entrée % Nice #%' OR name LIKE 'Dessert % Nice #%'
        ORDER BY id
        """
    ).fetchall()
    inserted_map = {row["name"]: row["id"] for row in inserted_rows}

    rows_to_insert = []
    for template_row, item_group in zip(filtered_templates, filtered_items):
        dish_name = template_row[0]
        dish_id = inserted_map.get(dish_name)
        if dish_id is None:
            continue
        for ingredient_name, quantity in item_group:
            ingredient_id = ingredient_ids.get(ingredient_name)
            if ingredient_id is None or quantity <= 0:
                continue
            rows_to_insert.append((dish_id, ingredient_id, round(float(quantity), 2)))

    if rows_to_insert:
        conn.executemany(
            """
            INSERT INTO dish_template_items (dish_id, ingredient_id, quantity)
            VALUES (?, ?, ?)
            """,
            rows_to_insert,
        )


def to_dict(row):
    return {k: row[k] for k in row.keys()}


def parse_diet_tokens(raw_value):
    return {token.strip().lower() for token in str(raw_value or "").split(",") if token.strip()}


def build_pricing_payload(conn):
    rows = conn.execute(
        """
        SELECT key, amount_eur, updated_at
        FROM pricing_rules
        ORDER BY key
        """
    ).fetchall()

    values = {key: float(amount) for key, amount in DEFAULT_PRICING_RULES.items()}
    updated_at = ""
    for row in rows:
        values[row["key"]] = float(row["amount_eur"])
        if row["updated_at"] and row["updated_at"] > updated_at:
            updated_at = row["updated_at"]

    return {
        "updated_at": updated_at or (datetime.utcnow().isoformat() + "Z"),
        "values": values,
    }


def get_inventory_rows(conn):
    query = """
        SELECT
            id,
            name,
            category,
            unit,
            stock,
            min_stock,
            co2_per_unit,
            cost_per_unit,
            provenance,
            supplier,
            diets,
            CASE WHEN stock <= min_stock THEN 1 ELSE 0 END AS low_stock
        FROM ingredients
        ORDER BY category, name
    """
    rows = conn.execute(query).fetchall()
    return [to_dict(row) for row in rows]


def get_catalog_rows(conn, limit=None, include_inactive=False, course=None, diet=None):
    where_parts = []
    params = []
    if not include_inactive:
        where_parts.append("COALESCE(is_active, 1) = 1")
    if course:
        where_parts.append("COALESCE(course, 'main') = ?")
        params.append(str(course).strip())
    if diet:
        where_parts.append("LOWER(diet) = LOWER(?)")
        params.append(str(diet).strip())

    where_clause = ""
    if where_parts:
        where_clause = " WHERE " + " AND ".join(where_parts)

    limit_clause = ""
    if limit is not None:
        try:
            safe_limit = int(limit)
            if safe_limit > 0:
                limit_clause = " LIMIT ?"
                params.append(safe_limit)
        except (TypeError, ValueError):
            pass

    dish_query = (
        "SELECT id, name, style, COALESCE(course, 'main') AS course, diet, story, image_url, "
        "COALESCE(is_active, 1) AS is_active, COALESCE(base_price_eur, 0) AS base_price_eur, "
        "COALESCE(updated_at, '') AS updated_at "
        "FROM dish_templates"
        + where_clause
        + " ORDER BY id"
        + limit_clause
    )
    dishes = [to_dict(row) for row in conn.execute(dish_query, params).fetchall()]
    if not dishes:
        return []

    dish_ids = [dish["id"] for dish in dishes]
    placeholders = ",".join(["?"] * len(dish_ids))
    item_query = f"""
        SELECT
            dti.dish_id,
            dti.ingredient_id,
            dti.quantity,
            i.name,
            i.category,
            i.unit,
            i.stock,
            i.min_stock,
            i.co2_per_unit,
            i.cost_per_unit,
            i.provenance,
            i.supplier,
            i.diets
        FROM dish_template_items dti
        JOIN ingredients i ON i.id = dti.ingredient_id
        WHERE dti.dish_id IN ({placeholders})
        ORDER BY dti.dish_id, dti.id
    """
    items = [to_dict(row) for row in conn.execute(item_query, dish_ids).fetchall()]

    grouped = {dish["id"]: {**dish, "composition": []} for dish in dishes}
    for item in items:
        grouped[item["dish_id"]]["composition"].append(
            {
                "ingredient_id": item["ingredient_id"],
                "name": item["name"],
                "category": item["category"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "stock": item["stock"],
                "min_stock": item["min_stock"],
                "co2_per_unit": item["co2_per_unit"],
                "cost_per_unit": item["cost_per_unit"],
                "provenance": item["provenance"],
                "supplier": item["supplier"],
                "diets": item["diets"],
            }
        )

    return list(grouped.values())


def summarize_dish_availability(dish):
    composition = dish.get("composition", [])
    if not composition:
        return {
            "available_now": False,
            "stock_level": "unknown",
            "shortages": [],
            "risk_count": 0,
        }

    shortages = []
    risk_count = 0
    for item in composition:
        quantity = float(item.get("quantity", 0))
        stock = float(item.get("stock", 0))
        min_stock = float(item.get("min_stock", 0))
        if stock < quantity:
            shortages.append(
                {
                    "ingredient_id": item.get("ingredient_id"),
                    "name": item.get("name"),
                    "required": round(quantity, 2),
                    "available": round(stock, 2),
                    "unit": item.get("unit"),
                }
            )
        if (stock - quantity) <= min_stock:
            risk_count += 1

    if shortages:
        stock_level = "critical"
    elif risk_count > 0:
        stock_level = "warning"
    else:
        stock_level = "ok"

    return {
        "available_now": len(shortages) == 0,
        "stock_level": stock_level,
        "shortages": shortages,
        "risk_count": risk_count,
    }


def get_admin_dish_rows(conn, course_filter="", diet_filter="", active_filter="", query=""):
    dishes = get_catalog_rows(conn, include_inactive=True)
    normalized_query = str(query or "").strip().lower()
    normalized_course = str(course_filter or "").strip().lower()
    normalized_diet = str(diet_filter or "").strip().lower()
    normalized_active = str(active_filter or "").strip().lower()

    filtered = []
    for dish in dishes:
        course = str(dish.get("course", "main")).strip().lower()
        diet = str(dish.get("diet", "")).strip().lower()
        is_active = int(dish.get("is_active", 1)) == 1

        if normalized_course and course != normalized_course:
            continue
        if normalized_diet and diet != normalized_diet:
            continue
        if normalized_active in {"active", "1"} and not is_active:
            continue
        if normalized_active in {"inactive", "0"} and is_active:
            continue
        if normalized_query:
            searchable = " ".join(
                [
                    str(dish.get("name", "")),
                    str(dish.get("style", "")),
                    str(dish.get("story", "")),
                    str(dish.get("course", "")),
                    str(dish.get("diet", "")),
                ]
            ).lower()
            if normalized_query not in searchable:
                continue

        availability = summarize_dish_availability(dish)
        food_cost = round(
            sum(float(item.get("quantity", 0)) * float(item.get("cost_per_unit", 0)) for item in dish["composition"]),
            2,
        )
        carbon = round(
            sum(float(item.get("quantity", 0)) * float(item.get("co2_per_unit", 0)) for item in dish["composition"]),
            3,
        )

        filtered.append(
            {
                "id": dish["id"],
                "name": dish["name"],
                "style": dish["style"],
                "course": dish["course"],
                "diet": dish["diet"],
                "story": dish["story"],
                "image_url": dish["image_url"],
                "is_active": 1 if is_active else 0,
                "base_price_eur": round(float(dish.get("base_price_eur", 0)), 2),
                "updated_at": dish.get("updated_at"),
                "ingredient_count": len(dish.get("composition", [])),
                "estimated_food_cost_eur": food_cost,
                "estimated_carbon_kg": carbon,
                "availability": availability,
            }
        )

    filtered.sort(key=lambda row: (row["course"], row["name"]))
    return filtered


def build_stock_insights(conn):
    ingredients = get_inventory_rows(conn)
    by_category = {}
    critical = []
    warning = []

    for item in ingredients:
        category = item["category"]
        by_category.setdefault(category, []).append(item)
        stock = float(item["stock"])
        min_stock = float(item["min_stock"])
        if stock <= min_stock:
            critical.append(item)
        elif min_stock > 0 and stock <= (min_stock * 1.25):
            warning.append(item)

    replacement_suggestions = []
    for item in critical[:25]:
        category_items = by_category.get(item["category"], [])
        required_tokens = parse_diet_tokens(item.get("diets", ""))
        candidates = []
        for candidate in category_items:
            if candidate["id"] == item["id"]:
                continue
            c_stock = float(candidate["stock"])
            c_min = float(candidate["min_stock"])
            if c_stock <= (c_min * 1.4):
                continue
            candidate_tokens = parse_diet_tokens(candidate.get("diets", ""))
            if required_tokens and candidate_tokens and not (required_tokens & candidate_tokens):
                continue
            candidates.append(candidate)

        candidates.sort(key=lambda row: (float(row["stock"]) - float(row["min_stock"])), reverse=True)
        best = candidates[:3]
        replacement_suggestions.append(
            {
                "ingredient_id": item["id"],
                "ingredient_name": item["name"],
                "category": item["category"],
                "alternatives": [
                    {
                        "ingredient_id": alt["id"],
                        "name": alt["name"],
                        "supplier": alt["supplier"],
                        "provenance": alt["provenance"],
                        "stock": round(float(alt["stock"]), 2),
                        "unit": alt["unit"],
                    }
                    for alt in best
                ],
            }
        )

    return {
        "critical_count": len(critical),
        "warning_count": len(warning),
        "critical": [
            {
                "id": item["id"],
                "name": item["name"],
                "category": item["category"],
                "stock": round(float(item["stock"]), 2),
                "min_stock": round(float(item["min_stock"]), 2),
                "unit": item["unit"],
                "supplier": item["supplier"],
                "provenance": item["provenance"],
            }
            for item in critical
        ],
        "warning": [
            {
                "id": item["id"],
                "name": item["name"],
                "category": item["category"],
                "stock": round(float(item["stock"]), 2),
                "min_stock": round(float(item["min_stock"]), 2),
                "unit": item["unit"],
                "supplier": item["supplier"],
                "provenance": item["provenance"],
            }
            for item in warning
        ],
        "replacement_suggestions": replacement_suggestions,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }


def json_response(handler, payload, status=200):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    for key, value in SECURITY_HEADERS.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(data)


def read_json_body(handler):
    content_length = int(handler.headers.get("Content-Length", 0))
    if content_length == 0:
        return {}
    raw = handler.rfile.read(content_length)
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


class InventoryHandler(BaseHTTPRequestHandler):
    server_version = "HistorIAInventory/1.0"

    def do_OPTIONS(self):
        json_response(self, {"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/health":
            return json_response(
                self,
                {
                    "service": "inventory",
                    "status": "ok",
                    "database": DB_PATH,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            )

        conn = get_conn()
        try:
            if path == "/api/ingredients":
                ingredients = get_inventory_rows(conn)
                return json_response(
                    self,
                    {
                        "count": len(ingredients),
                        "ingredients": ingredients,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/stock/summary":
                ingredients = get_inventory_rows(conn)
                total_items = len(ingredients)
                low_stock = [item for item in ingredients if item["low_stock"] == 1]
                total_units = round(sum(item["stock"] for item in ingredients), 2)
                return json_response(
                    self,
                    {
                        "total_ingredients": total_items,
                        "total_units": total_units,
                        "low_stock_count": len(low_stock),
                        "low_stock": low_stock[:8],
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/stock/insights":
                insights = build_stock_insights(conn)
                return json_response(
                    self,
                    {
                        **insights,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/dishes/catalog":
                limit = None
                if "limit" in params:
                    try:
                        limit = int(params["limit"][0])
                    except (TypeError, ValueError):
                        limit = None
                catalog = get_catalog_rows(conn, limit=limit)
                return json_response(
                    self,
                    {
                        "count": len(catalog),
                        "catalog": catalog,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/dishes/admin":
                course = params.get("course", [""])[0]
                diet = params.get("diet", [""])[0]
                active = params.get("active", [""])[0]
                q = params.get("q", [""])[0]
                dishes = get_admin_dish_rows(
                    conn,
                    course_filter=course,
                    diet_filter=diet,
                    active_filter=active,
                    query=q,
                )
                return json_response(
                    self,
                    {
                        "count": len(dishes),
                        "dishes": dishes,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/pricing":
                pricing = build_pricing_payload(conn)
                return json_response(
                    self,
                    {
                        "pricing": pricing["values"],
                        "updated_at": pricing["updated_at"],
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/metrics":
                ingredient_count = conn.execute("SELECT COUNT(*) AS c FROM ingredients").fetchone()["c"]
                dishes_count = conn.execute("SELECT COUNT(*) AS c FROM dish_templates").fetchone()["c"]
                active_dishes_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM dish_templates WHERE COALESCE(is_active, 1) = 1"
                ).fetchone()["c"]
                by_course = {
                    row["course"]: row["c"]
                    for row in conn.execute(
                        """
                        SELECT COALESCE(course, 'main') AS course, COUNT(*) AS c
                        FROM dish_templates
                        GROUP BY COALESCE(course, 'main')
                        """
                    ).fetchall()
                }
                return json_response(
                    self,
                    {
                        "ingredients": ingredient_count,
                        "dish_templates": dishes_count,
                        "active_dish_templates": active_dishes_count,
                        "courses": by_course,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            if path == "/api/ops/status":
                pricing = build_pricing_payload(conn)
                insights = build_stock_insights(conn)
                return json_response(
                    self,
                    {
                        "service": "inventory",
                        "status": "ok",
                        "database_engine": "postgres" if USING_POSTGRES else "sqlite",
                        "low_stock_count": insights["critical_count"],
                        "warning_stock_count": insights["warning_count"],
                        "pricing_updated_at": pricing["updated_at"],
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )

            return json_response(self, {"error": "Route introuvable"}, status=404)
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = read_json_body(self)
        if body is None:
            return json_response(self, {"error": "JSON invalide"}, status=400)

        if path == "/api/stock/consume":
            items = body.get("items", [])
            if not isinstance(items, list) or not items:
                return json_response(self, {"error": "items est obligatoire"}, status=400)

            normalized = []
            for item in items:
                try:
                    ingredient_id = int(item["ingredient_id"])
                    quantity = float(item["quantity"])
                    if quantity <= 0:
                        raise ValueError("quantité <= 0")
                    normalized.append((ingredient_id, quantity))
                except (KeyError, TypeError, ValueError):
                    return json_response(
                        self,
                        {"error": "Chaque item doit contenir ingredient_id et quantity > 0"},
                        status=400,
                    )

            conn = get_conn()
            try:
                conn.begin()
                shortages = []
                for ingredient_id, quantity in normalized:
                    row = conn.execute(
                        "SELECT id, name, stock, unit FROM ingredients WHERE id = ?",
                        (ingredient_id,),
                    ).fetchone()
                    if row is None:
                        shortages.append(
                            {
                                "ingredient_id": ingredient_id,
                                "name": "Inconnu",
                                "required": quantity,
                                "available": 0,
                            }
                        )
                        continue
                    if row["stock"] < quantity:
                        shortages.append(
                            {
                                "ingredient_id": row["id"],
                                "name": row["name"],
                                "required": quantity,
                                "available": row["stock"],
                                "unit": row["unit"],
                            }
                        )

                if shortages:
                    conn.rollback()
                    return json_response(
                        self,
                        {
                            "error": "Stock insuffisant",
                            "shortages": shortages,
                        },
                        status=409,
                    )

                for ingredient_id, quantity in normalized:
                    conn.execute(
                        "UPDATE ingredients SET stock = stock - ? WHERE id = ?",
                        (quantity, ingredient_id),
                    )

                conn.commit()

                updated = []
                for ingredient_id, _ in normalized:
                    row = conn.execute(
                        """
                        SELECT id, name, stock, min_stock, unit,
                               CASE WHEN stock <= min_stock THEN 1 ELSE 0 END AS low_stock
                        FROM ingredients WHERE id = ?
                        """,
                        (ingredient_id,),
                    ).fetchone()
                    if row:
                        updated.append(to_dict(row))

                return json_response(
                    self,
                    {
                        "status": "consumed",
                        "updated": updated,
                        "timestamp": datetime.utcnow().isoformat() + "Z",
                    },
                )
            finally:
                conn.close()

        if path == "/api/stock/restock":
            try:
                ingredient_id = int(body.get("ingredient_id"))
                quantity = float(body.get("quantity"))
                if quantity <= 0:
                    raise ValueError("quantity")
            except (TypeError, ValueError):
                return json_response(self, {"error": "ingredient_id et quantity > 0 requis"}, status=400)

            conn = get_conn()
            try:
                conn.execute(
                    "UPDATE ingredients SET stock = stock + ? WHERE id = ?",
                    (quantity, ingredient_id),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT id, name, stock, unit FROM ingredients WHERE id = ?",
                    (ingredient_id,),
                ).fetchone()
                if row is None:
                    return json_response(self, {"error": "Ingrédient introuvable"}, status=404)
                return json_response(self, {"status": "restocked", "ingredient": to_dict(row)})
            finally:
                conn.close()

        if path == "/api/stock/set":
            try:
                ingredient_id = int(body.get("ingredient_id"))
                stock = float(body.get("stock"))
                if stock < 0:
                    raise ValueError("stock")
            except (TypeError, ValueError):
                return json_response(self, {"error": "ingredient_id et stock >= 0 requis"}, status=400)

            conn = get_conn()
            try:
                updated = conn.execute(
                    "UPDATE ingredients SET stock = ? WHERE id = ?",
                    (stock, ingredient_id),
                )
                if updated.rowcount == 0:
                    return json_response(self, {"error": "Ingrédient introuvable"}, status=404)
                conn.commit()
                row = conn.execute(
                    "SELECT id, name, stock, min_stock, unit FROM ingredients WHERE id = ?",
                    (ingredient_id,),
                ).fetchone()
                return json_response(self, {"status": "stock_set", "ingredient": to_dict(row)})
            finally:
                conn.close()

        if path == "/api/ingredient/update":
            try:
                ingredient_id = int(body.get("ingredient_id"))
            except (TypeError, ValueError):
                return json_response(self, {"error": "ingredient_id requis"}, status=400)

            allowed_fields = {
                "name": ("text",),
                "category": ("text",),
                "unit": ("text",),
                "min_stock": ("float", 0.0),
                "co2_per_unit": ("float", 0.0),
                "cost_per_unit": ("float", 0.0),
                "provenance": ("text",),
                "supplier": ("text",),
                "diets": ("text",),
            }

            updates = []
            params = []

            for field, spec in allowed_fields.items():
                if field not in body:
                    continue
                value = body[field]
                if spec[0] == "text":
                    if value is None:
                        continue
                    value = str(value).strip()
                    if value == "":
                        return json_response(self, {"error": f"{field} ne peut pas être vide"}, status=400)
                    updates.append(f"{field} = ?")
                    params.append(value)
                else:
                    try:
                        numeric = float(value)
                    except (TypeError, ValueError):
                        return json_response(self, {"error": f"{field} doit être numérique"}, status=400)
                    if numeric < spec[1]:
                        return json_response(self, {"error": f"{field} doit être >= {spec[1]}"}, status=400)
                    updates.append(f"{field} = ?")
                    params.append(numeric)

            if not updates:
                return json_response(self, {"error": "Aucun champ à mettre à jour"}, status=400)

            params.append(ingredient_id)
            conn = get_conn()
            try:
                query = f"UPDATE ingredients SET {', '.join(updates)} WHERE id = ?"
                updated = conn.execute(query, params)
                if updated.rowcount == 0:
                    return json_response(self, {"error": "Ingrédient introuvable"}, status=404)
                conn.commit()
                row = conn.execute(
                    """
                    SELECT
                        id, name, category, unit, stock, min_stock, co2_per_unit,
                        cost_per_unit, provenance, supplier, diets,
                        CASE WHEN stock <= min_stock THEN 1 ELSE 0 END AS low_stock
                    FROM ingredients
                    WHERE id = ?
                    """,
                    (ingredient_id,),
                ).fetchone()
                return json_response(self, {"status": "ingredient_updated", "ingredient": to_dict(row)})
            finally:
                conn.close()

        if path == "/api/ingredient/create":
            required_text_fields = ["name", "category", "unit", "provenance", "supplier", "diets"]
            for field in required_text_fields:
                value = body.get(field)
                if value is None or str(value).strip() == "":
                    return json_response(self, {"error": f"{field} est requis"}, status=400)

            try:
                stock = float(body.get("stock"))
                min_stock = float(body.get("min_stock"))
                co2_per_unit = float(body.get("co2_per_unit"))
                cost_per_unit = float(body.get("cost_per_unit"))
            except (TypeError, ValueError):
                return json_response(
                    self,
                    {"error": "stock, min_stock, co2_per_unit et cost_per_unit doivent être numériques"},
                    status=400,
                )

            if min(stock, min_stock, co2_per_unit, cost_per_unit) < 0:
                return json_response(self, {"error": "Les valeurs numériques doivent être >= 0"}, status=400)

            conn = get_conn()
            try:
                params = (
                    str(body["name"]).strip(),
                    str(body["category"]).strip(),
                    str(body["unit"]).strip(),
                    stock,
                    min_stock,
                    co2_per_unit,
                    cost_per_unit,
                    str(body["provenance"]).strip(),
                    str(body["supplier"]).strip(),
                    str(body["diets"]).strip(),
                )

                if USING_POSTGRES:
                    row = conn.execute(
                        """
                        INSERT INTO ingredients (
                            name, category, unit, stock, min_stock, co2_per_unit,
                            cost_per_unit, provenance, supplier, diets
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        RETURNING
                            id, name, category, unit, stock, min_stock, co2_per_unit,
                            cost_per_unit, provenance, supplier, diets
                        """,
                        params,
                    ).fetchone()
                else:
                    cursor = conn.execute(
                        """
                        INSERT INTO ingredients (
                            name, category, unit, stock, min_stock, co2_per_unit,
                            cost_per_unit, provenance, supplier, diets
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        params,
                    )
                    ingredient_id = cursor.lastrowid
                    row = conn.execute(
                        """
                        SELECT
                            id, name, category, unit, stock, min_stock, co2_per_unit,
                            cost_per_unit, provenance, supplier, diets
                        FROM ingredients
                        WHERE id = ?
                        """,
                        (ingredient_id,),
                    ).fetchone()
                conn.commit()
                return json_response(self, {"status": "ingredient_created", "ingredient": to_dict(row)}, status=201)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                return json_response(self, {"error": f"Création impossible: {exc}"}, status=409)
            finally:
                conn.close()

        if path == "/api/dish/update":
            try:
                dish_id = int(body.get("dish_id"))
            except (TypeError, ValueError):
                return json_response(self, {"error": "dish_id requis"}, status=400)

            allowed_fields = {
                "name": "text",
                "style": "text",
                "course": "course",
                "diet": "diet",
                "story": "text",
                "image_url": "text",
                "is_active": "bool",
                "base_price_eur": "price",
            }

            updates = []
            params = []
            for field, mode in allowed_fields.items():
                if field not in body:
                    continue
                raw_value = body.get(field)
                if mode == "text":
                    value = str(raw_value or "").strip()
                    if not value:
                        return json_response(self, {"error": f"{field} ne peut pas être vide"}, status=400)
                    updates.append(f"{field} = ?")
                    params.append(value)
                elif mode == "course":
                    value = str(raw_value or "").strip().lower()
                    if value not in {"entree", "main", "dessert"}:
                        return json_response(self, {"error": "course invalide"}, status=400)
                    updates.append("course = ?")
                    params.append(value)
                elif mode == "diet":
                    value = str(raw_value or "").strip().lower()
                    if value not in {"omnivore", "pescatarian", "vegetarian", "vegan"}:
                        return json_response(self, {"error": "diet invalide"}, status=400)
                    updates.append("diet = ?")
                    params.append(value)
                elif mode == "bool":
                    value = raw_value
                    if isinstance(value, str):
                        value = value.strip().lower() in {"1", "true", "yes", "oui", "on"}
                    updates.append("is_active = ?")
                    params.append(1 if value else 0)
                elif mode == "price":
                    try:
                        numeric = float(raw_value)
                    except (TypeError, ValueError):
                        return json_response(self, {"error": "base_price_eur doit être numérique"}, status=400)
                    if numeric < 0:
                        return json_response(self, {"error": "base_price_eur doit être >= 0"}, status=400)
                    updates.append("base_price_eur = ?")
                    params.append(round(numeric, 2))

            if not updates:
                return json_response(self, {"error": "Aucun champ à mettre à jour"}, status=400)

            now_iso = datetime.utcnow().isoformat() + "Z"
            updates.append("updated_at = ?")
            params.append(now_iso)
            params.append(dish_id)

            conn = get_conn()
            try:
                query = f"UPDATE dish_templates SET {', '.join(updates)} WHERE id = ?"
                updated = conn.execute(query, params)
                if updated.rowcount == 0:
                    return json_response(self, {"error": "Plat introuvable"}, status=404)
                conn.commit()

                dishes = get_catalog_rows(conn, include_inactive=True)
                dish = next((row for row in dishes if int(row["id"]) == dish_id), None)
                if not dish:
                    return json_response(self, {"error": "Plat introuvable après mise à jour"}, status=404)

                availability = summarize_dish_availability(dish)
                payload = {
                    "id": dish["id"],
                    "name": dish["name"],
                    "style": dish["style"],
                    "course": dish["course"],
                    "diet": dish["diet"],
                    "story": dish["story"],
                    "image_url": dish["image_url"],
                    "is_active": dish["is_active"],
                    "base_price_eur": round(float(dish.get("base_price_eur", 0)), 2),
                    "updated_at": dish.get("updated_at"),
                    "ingredient_count": len(dish.get("composition", [])),
                    "availability": availability,
                }
                return json_response(self, {"status": "dish_updated", "dish": payload})
            finally:
                conn.close()

        if path == "/api/pricing/set":
            key = str(body.get("key", "")).strip().lower()
            if key not in DEFAULT_PRICING_RULES:
                return json_response(
                    self,
                    {"error": "Clé de pricing invalide"},
                    status=400,
                )
            try:
                amount = float(body.get("amount_eur"))
            except (TypeError, ValueError):
                return json_response(self, {"error": "amount_eur doit être numérique"}, status=400)
            if amount <= 0:
                return json_response(self, {"error": "amount_eur doit être > 0"}, status=400)

            now_iso = datetime.utcnow().isoformat() + "Z"
            conn = get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO pricing_rules (key, amount_eur, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET amount_eur = excluded.amount_eur, updated_at = excluded.updated_at
                    """,
                    (key, round(amount, 2), now_iso),
                )

                course_map = {
                    "single_entree": "entree",
                    "single_main": "main",
                    "single_dessert": "dessert",
                }
                course = course_map.get(key)
                if course:
                    conn.execute(
                        """
                        UPDATE dish_templates
                        SET base_price_eur = ?, updated_at = ?
                        WHERE COALESCE(course, 'main') = ?
                        """,
                        (round(amount, 2), now_iso, course),
                    )

                conn.commit()
                pricing = build_pricing_payload(conn)
                return json_response(
                    self,
                    {
                        "status": "pricing_updated",
                        "pricing": pricing["values"],
                        "updated_at": pricing["updated_at"],
                    },
                )
            finally:
                conn.close()

        return json_response(self, {"error": "Route introuvable"}, status=404)

    def log_message(self, format_str, *args):
        print(f"[inventory] {self.address_string()} - {format_str % args}")


def main():
    init_db()
    seed_data_if_needed()
    conn = get_conn()
    try:
        localize_dataset_to_nice(conn)
        ensure_extended_ingredients(conn)
        ensure_menu_course_templates(conn)
        ensure_dish_template_management_columns(conn)
        ensure_pricing_rules_table(conn)
        conn.commit()
    finally:
        conn.close()
    server = ThreadingHTTPServer((HOST, PORT), InventoryHandler)
    print(f"Inventory server running on http://{HOST}:{PORT}")
    if USING_POSTGRES:
        print(f"DB engine: postgres ({DATABASE_URL})")
    else:
        print(f"DB engine: sqlite ({DB_PATH})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
