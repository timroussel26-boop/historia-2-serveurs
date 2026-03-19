#!/usr/bin/env python3
import ipaddress
import json
import mimetypes
import os
import random
import re
import sqlite3
import unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
ORDERS_DB_PATH = os.environ.get("ORDERS_DB_PATH", os.path.join(BASE_DIR, "historia_orders.db")).strip()
if not ORDERS_DB_PATH:
    ORDERS_DB_PATH = os.path.join(BASE_DIR, "historia_orders.db")
HOST = os.environ.get("AI_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_PORT", "4200"))
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://127.0.0.1:4100")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ADMIN_ALLOWED_IPS = os.environ.get(
    "ADMIN_ALLOWED_IPS",
    "127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7,fe80::/10",
).strip()

CATALOG_CACHE = {"value": None, "at": None}
INGREDIENTS_CACHE = {"value": None, "at": None}
PRICING_CACHE = {"value": None, "at": None}
CACHE_SECONDS = 5
MENU_LABELS = {
    "decouverte": "Menu Découverte",
    "signature": "Menu Signature",
}
MENU_PRICES_DEFAULT = {
    "decouverte": 30.0,
    "signature": 36.0,
}
SINGLE_PRICES_DEFAULT = {
    "entree": 11.0,
    "main": 24.0,
    "dessert": 8.0,
}
DISCOVERY_OPTIONS = {
    "entree_main": ("Entrée + Plat", ["entree", "main"]),
    "main_dessert": ("Plat + Dessert", ["main", "dessert"]),
}
COURSE_LABELS = {
    "entree": "Entrée",
    "main": "Plat",
    "dessert": "Dessert",
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def normalize_text(value):
    raw = unicodedata.normalize("NFKD", value or "")
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower().strip()
    raw = re.sub(r"[^a-z0-9\s'-]", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw


def normalize_choice_key(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def parse_admin_networks():
    networks = []
    for token in ADMIN_ALLOWED_IPS.split(","):
        candidate = token.strip()
        if not candidate:
            continue
        try:
            if "/" in candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
            else:
                ip = ipaddress.ip_address(candidate)
                suffix = "/32" if ip.version == 4 else "/128"
                networks.append(ipaddress.ip_network(f"{candidate}{suffix}", strict=False))
        except ValueError:
            continue
    return networks


ADMIN_ALLOWED_NETWORKS = parse_admin_networks()


def extract_client_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = handler.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    return (handler.client_address or ("", 0))[0]


def admin_access_allowed(handler):
    if not ADMIN_ALLOWED_NETWORKS:
        return True
    candidate = extract_client_ip(handler)
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return any(ip in network for network in ADMIN_ALLOWED_NETWORKS)


def require_admin_network(handler):
    if admin_access_allowed(handler):
        return True
    json_response(
        handler,
        {"error": "Accès admin refusé depuis cette adresse IP"},
        status=403,
    )
    return False


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
    body = handler.rfile.read(content_length)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def binary_response(handler, payload, content_type, filename=None, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    if filename:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    for key, value in SECURITY_HEADERS.items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(payload)


def get_orders_conn():
    conn = sqlite3.connect(ORDERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_orders_db():
    conn = get_orders_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                service_date TEXT NOT NULL,
                table_number TEXT,
                menu_label TEXT NOT NULL,
                pairing TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                price_eur REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                dish_id INTEGER NOT NULL,
                dish_name TEXT NOT NULL,
                course TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def make_ticket_no(stamp=None):
    now = stamp or datetime.utcnow()
    return now.strftime("HTA-%Y%m%d-%H%M%S-") + "".join(
        random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(3)
    )


def log_order_for_exports(
    ticket_no,
    created_at,
    table_number,
    menu_label,
    pairing,
    party_size,
    price_eur,
    ordered_dishes,
):
    service_date = created_at[:10]
    conn = get_orders_conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO orders (
                ticket_no, created_at, service_date, table_number, menu_label, pairing, party_size, price_eur
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_no,
                created_at,
                service_date,
                table_number or "",
                menu_label or "Formule HistorIA",
                pairing or "",
                int(party_size),
                float(price_eur),
            ),
        )
        order_id = int(cursor.lastrowid)
        rows = []
        for dish in ordered_dishes:
            rows.append(
                (
                    order_id,
                    int(dish.get("dish_id", 0)),
                    str(dish.get("name", "Plat")).strip(),
                    str(dish.get("course", "main")).strip(),
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO order_lines (order_id, dish_id, dish_name, course)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
        conn.commit()
    finally:
        conn.close()


def parse_service_date(value):
    raw = (value or "").strip()
    if not raw:
        return datetime.utcnow().strftime("%Y-%m-%d")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def fetch_orders_for_date(service_date):
    conn = get_orders_conn()
    try:
        order_rows = conn.execute(
            """
            SELECT id, ticket_no, created_at, service_date, table_number, menu_label, pairing, party_size, price_eur
            FROM orders
            WHERE service_date = ?
            ORDER BY created_at ASC, id ASC
            """,
            (service_date,),
        ).fetchall()

        if not order_rows:
            return []

        order_ids = [row["id"] for row in order_rows]
        placeholders = ",".join("?" for _ in order_ids)
        line_rows = conn.execute(
            f"""
            SELECT order_id, dish_id, dish_name, course
            FROM order_lines
            WHERE order_id IN ({placeholders})
            ORDER BY order_id ASC, id ASC
            """,
            order_ids,
        ).fetchall()

        grouped_lines = {}
        for line in line_rows:
            grouped_lines.setdefault(line["order_id"], []).append(
                {
                    "dish_id": line["dish_id"],
                    "dish_name": line["dish_name"],
                    "course": line["course"],
                }
            )

        result = []
        for row in order_rows:
            result.append(
                {
                    "ticket_no": row["ticket_no"],
                    "created_at": row["created_at"],
                    "service_date": row["service_date"],
                    "table_number": row["table_number"],
                    "menu_label": row["menu_label"],
                    "pairing": row["pairing"],
                    "party_size": row["party_size"],
                    "price_eur": round(float(row["price_eur"]), 2),
                    "dishes": grouped_lines.get(row["id"], []),
                }
            )
        return result
    finally:
        conn.close()


def build_orders_csv(service_date, orders):
    lines = [
        "date_service,ticket,heure,table,formule,pairing,couverts,montant_eur,plats",
    ]
    for row in orders:
        created_at = row.get("created_at", "")
        hour = created_at[11:19] if len(created_at) >= 19 else created_at
        dishes = " | ".join(
            f"{item.get('course', '')}:{item.get('dish_name', '')}" for item in row.get("dishes", [])
        )
        values = [
            service_date,
            row.get("ticket_no", ""),
            hour,
            row.get("table_number", ""),
            row.get("menu_label", ""),
            row.get("pairing", ""),
            str(row.get("party_size", "")),
            f"{float(row.get('price_eur', 0)):.2f}",
            dishes.replace(",", " "),
        ]
        lines.append(",".join(values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _pdf_escape(value):
    return str(value or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_orders_pdf(service_date, orders):
    total = round(sum(float(row.get("price_eur", 0)) for row in orders), 2)
    lines = [
        "HistorIA - Export commandes",
        f"Date service: {service_date}",
        f"Total commandes: {len(orders)}",
        f"Total CA: {total:.2f} EUR",
        "",
    ]
    for row in orders:
        created_at = row.get("created_at", "")
        hour = created_at[11:19] if len(created_at) >= 19 else created_at
        lines.append(
            f"{row.get('ticket_no', '')} | {hour} | Table {row.get('table_number') or '-'} | "
            f"{row.get('menu_label', '')} | {float(row.get('price_eur', 0)):.2f} EUR"
        )
        for dish in row.get("dishes", []):
            lines.append(f"  - {dish.get('course', '')}: {dish.get('dish_name', '')}")
        lines.append("")

    if not lines:
        lines = ["HistorIA - Export commandes", f"Date service: {service_date}", "Aucune commande"]

    lines_per_page = 42
    pages = [lines[idx : idx + lines_per_page] for idx in range(0, len(lines), lines_per_page)]
    if not pages:
        pages = [["HistorIA - Export commandes", f"Date service: {service_date}", "Aucune commande"]]

    objects = []
    pages_ids = []
    next_obj_id = 4
    for page_lines in pages:
        content_lines = ["BT", "/F1 10 Tf", "50 792 Td", "14 TL"]
        first = True
        for text_line in page_lines:
            escaped = _pdf_escape(text_line)
            if first:
                content_lines.append(f"({escaped}) Tj")
                first = False
            else:
                content_lines.append(f"T* ({escaped}) Tj")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("utf-8")
        content_obj = next_obj_id
        page_obj = next_obj_id + 1
        next_obj_id += 2
        objects.append((content_obj, f"<< /Length {len(stream)} >>\nstream\n".encode("utf-8") + stream + b"\nendstream"))
        objects.append(
            (
                page_obj,
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 1 0 R >> >> /Contents {content_obj} 0 R >>"
                ).encode("utf-8"),
            )
        )
        pages_ids.append(page_obj)

    kids = " ".join(f"{page_id} 0 R" for page_id in pages_ids)
    page_tree = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages_ids)} >>".encode("utf-8")
    catalog = b"<< /Type /Catalog /Pages 2 0 R >>"
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    core_objects = [(1, font), (2, page_tree), (3, catalog)]
    all_objects = core_objects + objects

    pdf_parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets = {}
    current_offset = len(pdf_parts[0])
    for obj_id, obj_content in all_objects:
        obj_prefix = f"{obj_id} 0 obj\n".encode("utf-8")
        obj_suffix = b"\nendobj\n"
        offsets[obj_id] = current_offset
        pdf_parts.append(obj_prefix)
        pdf_parts.append(obj_content)
        pdf_parts.append(obj_suffix)
        current_offset += len(obj_prefix) + len(obj_content) + len(obj_suffix)

    xref_offset = current_offset
    max_obj_id = max(obj_id for obj_id, _ in all_objects)
    xref_entries = [b"0000000000 65535 f \n"]
    for obj_id in range(1, max_obj_id + 1):
        offset = offsets.get(obj_id, 0)
        xref_entries.append(f"{offset:010d} 00000 n \n".encode("utf-8"))

    pdf_parts.append(f"xref\n0 {max_obj_id + 1}\n".encode("utf-8"))
    pdf_parts.extend(xref_entries)
    pdf_parts.append(
        (
            f"trailer\n<< /Size {max_obj_id + 1} /Root 3 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("utf-8")
    )

    return b"".join(pdf_parts)


def inventory_request(path, method="GET", payload=None):
    url = INVENTORY_URL.rstrip("/") + path
    body = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(url, method=method, data=body, headers=headers)
    try:
        with request.urlopen(req, timeout=8) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, json.loads(data)
    except error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:
            return exc.code, {"error": f"Inventory HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return 503, {"error": f"Inventory indisponible: {exc}"}


def extract_openai_text(payload):
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    for item in payload.get("output", []):
        for content in item.get("content", []):
            text_value = content.get("text")
            if isinstance(text_value, str) and text_value.strip():
                return text_value.strip()
    return ""


def openai_generate_copy(dish, preferences):
    if not OPENAI_API_KEY:
        return None

    composition = ", ".join(
        f"{part['name']} ({part['quantity']} {part['unit']})"
        for part in dish.get("composition", [])[:8]
    )
    user_payload = {
        "dish_name": dish.get("name"),
        "style": dish.get("style"),
        "diet": dish.get("diet"),
        "carbon_kg": dish.get("carbon_footprint_kg"),
        "price_eur": dish.get("price_estimate_eur"),
        "provenance": dish.get("provenance", []),
        "composition": composition,
        "client_goal": preferences.get("goal", "anti_waste"),
    }

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "description": {"type": "string"},
            "story": {"type": "string"},
            "service_pitch": {"type": "string"},
            "plating_notes": {"type": "string"},
            "chef_note": {"type": "string"},
        },
        "required": ["description", "story", "service_pitch", "plating_notes", "chef_note"],
    }

    request_body = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Tu es un chef narratif francophone. Rédige un texte premium court, "
                            "précis et orienté restauration. Ne pas inventer de nouvel ingrédient."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(user_payload, ensure_ascii=False),
                    }
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "dish_copy",
                "schema": schema,
                "strict": True,
            }
        },
        "temperature": 0.8,
        "max_output_tokens": 360,
    }

    req = request.Request(
        f"{OPENAI_BASE_URL}/responses",
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps(request_body).encode("utf-8"),
    )

    try:
        with request.urlopen(req, timeout=14) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    text = extract_openai_text(payload)
    if not text:
        return None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    required = ["description", "story", "service_pitch", "plating_notes", "chef_note"]
    if not all(isinstance(parsed.get(key), str) and parsed.get(key).strip() for key in required):
        return None
    return parsed


def openai_generate_menu_copy(menu_payload, preferences):
    if not OPENAI_API_KEY:
        return None

    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "menu_headline": {"type": "string"},
            "menu_story": {"type": "string"},
            "service_pitch": {"type": "string"},
        },
        "required": ["menu_headline", "menu_story", "service_pitch"],
    }

    request_body = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Tu es chef de rang dans un restaurant niçois. "
                            "Rédige une narration cohérente entre entrée, plat et dessert "
                            "avec un ton premium mais simple. N'invente aucun ingrédient."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            {
                                "menu": menu_payload,
                                "client_goal": preferences.get("goal", "anti_waste"),
                                "diet": preferences.get("diet", "omnivore"),
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "menu_copy",
                "schema": schema,
                "strict": True,
            }
        },
        "temperature": 0.7,
        "max_output_tokens": 320,
    }

    req = request.Request(
        f"{OPENAI_BASE_URL}/responses",
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        data=json.dumps(request_body).encode("utf-8"),
    )

    try:
        with request.urlopen(req, timeout=14) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    text = extract_openai_text(payload)
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    required = ["menu_headline", "menu_story", "service_pitch"]
    if not all(isinstance(parsed.get(key), str) and parsed.get(key).strip() for key in required):
        return None
    return parsed


def build_menu_fallback_copy(menu_label, menu_pairing, selected_courses, goal):
    course_names = ", ".join(
        f"{COURSE_LABELS.get(item.get('course', 'main'), 'Plat')}: {item.get('name', 'Suggestion du jour')}"
        for item in selected_courses
    )
    goal_labels = {
        "anti_waste": "anti-gaspillage",
        "low_carbon": "faible impact carbone",
        "high_protein": "profil plus protéiné",
        "comfort": "esprit gourmand",
    }
    goal_label = goal_labels.get(normalize_choice_key(goal), "équilibre cuisine/stock")
    return {
        "menu_headline": f"{menu_label} - {menu_pairing}",
        "menu_story": (
            f"Une proposition cohérente autour de {course_names}. "
            f"La sélection est optimisée pour un service niçois avec priorité {goal_label}."
        ),
        "service_pitch": (
            "Service recommandé: présenter l'ensemble du menu, puis lancer les assiettes dans l'ordre des courses."
        ),
    }


def normalize_diet(value):
    candidate = normalize_choice_key(value or "")
    aliases = {
        "omnivore": "omnivore",
        "omni": "omnivore",
        "pescatarian": "pescatarian",
        "pescetarien": "pescatarian",
        "poisson_uniquement": "pescatarian",
        "vegetarian": "vegetarian",
        "vegetarien": "vegetarian",
        "vegan": "vegan",
        "vegane": "vegan",
    }
    return aliases.get(candidate, "omnivore")


def parse_diets_csv(raw_value):
    return {normalize_choice_key(item) for item in str(raw_value or "").split(",") if item.strip()}


def diet_allows(requested_diet, template_diet, course="main"):
    requested = normalize_diet(requested_diet)
    template = normalize_diet(template_diet)
    # Filtre strict: un client omnivore obtient des plats omnivores uniquement, etc.
    rules = {
        "omnivore": {"omnivore"},
        "pescatarian": {"pescatarian"},
        "vegetarian": {"vegetarian"},
        "vegan": {"vegan"},
    }
    return template in rules.get(requested, {requested})


def ingredient_allows_diet(requested_diet, ingredient_diets):
    requested = normalize_diet(requested_diet)
    tokens = parse_diets_csv(ingredient_diets)
    if not tokens:
        return True
    return requested in tokens


def template_diet_consistent(template, requested_diet):
    if not diet_allows(requested_diet, template.get("diet", ""), template.get("course", "main")):
        return False
    for item in template.get("composition", []):
        if not ingredient_allows_diet(requested_diet, item.get("diets", "")):
            return False
    return True


def load_catalog(force=False):
    now = datetime.utcnow().timestamp()
    if (
        not force
        and CATALOG_CACHE["value"] is not None
        and CATALOG_CACHE["at"] is not None
        and (now - CATALOG_CACHE["at"]) < CACHE_SECONDS
    ):
        return CATALOG_CACHE["value"]

    status, payload = inventory_request("/api/dishes/catalog")
    if status != 200:
        raise RuntimeError(payload.get("error", "Impossible de récupérer le catalogue"))

    catalog = payload.get("catalog", [])
    CATALOG_CACHE["value"] = catalog
    CATALOG_CACHE["at"] = now
    return catalog


def load_ingredients(force=False):
    now = datetime.utcnow().timestamp()
    if (
        not force
        and INGREDIENTS_CACHE["value"] is not None
        and INGREDIENTS_CACHE["at"] is not None
        and (now - INGREDIENTS_CACHE["at"]) < CACHE_SECONDS
    ):
        return INGREDIENTS_CACHE["value"]

    status, payload = inventory_request("/api/ingredients")
    if status != 200:
        raise RuntimeError(payload.get("error", "Impossible de récupérer les ingrédients"))

    ingredients = payload.get("ingredients", [])
    INGREDIENTS_CACHE["value"] = ingredients
    INGREDIENTS_CACHE["at"] = now
    return ingredients


def load_pricing(force=False):
    now = datetime.utcnow().timestamp()
    if (
        not force
        and PRICING_CACHE["value"] is not None
        and PRICING_CACHE["at"] is not None
        and (now - PRICING_CACHE["at"]) < CACHE_SECONDS
    ):
        return PRICING_CACHE["value"]

    defaults = {
        "menu_decouverte": MENU_PRICES_DEFAULT["decouverte"],
        "menu_signature": MENU_PRICES_DEFAULT["signature"],
        "single_entree": SINGLE_PRICES_DEFAULT["entree"],
        "single_main": SINGLE_PRICES_DEFAULT["main"],
        "single_dessert": SINGLE_PRICES_DEFAULT["dessert"],
    }
    status, payload = inventory_request("/api/pricing")
    if status != 200:
        PRICING_CACHE["value"] = defaults
        PRICING_CACHE["at"] = now
        return defaults

    values = payload.get("pricing") or {}
    merged = {**defaults}
    for key in defaults:
        try:
            if key in values:
                merged[key] = float(values[key])
        except (TypeError, ValueError):
            continue
    PRICING_CACHE["value"] = merged
    PRICING_CACHE["at"] = now
    return merged


def compute_metrics(template, party_size):
    calories_by_category = {
        "protein": 1.9,
        "base": 1.3,
        "vegetable": 0.45,
        "sauce": 1.1,
        "herb": 0.15,
        "garnish": 2.2,
        "dessert": 2.0,
    }

    carbon = 0.0
    food_cost = 0.0
    calories = 0.0
    protein_mass = 0.0
    anti_waste = 0.0
    available = True
    enriched = []
    origins = set()

    for item in template.get("composition", []):
        quantity = float(item["quantity"]) * party_size
        stock = float(item["stock"])
        min_stock = float(item["min_stock"])

        if stock < quantity:
            available = False

        carbon += quantity * float(item["co2_per_unit"])
        food_cost += quantity * float(item["cost_per_unit"])

        category = item.get("category", "base")
        if item.get("unit") == "piece":
            calories += 70 * quantity
        else:
            calories += quantity * calories_by_category.get(category, 1.0)

        if category == "protein" and item.get("unit") != "piece":
            protein_mass += quantity
        elif category == "protein" and item.get("unit") == "piece":
            protein_mass += quantity * 50

        anti_waste += max(0.0, (stock - min_stock) / max(min_stock, 1.0))

        origins.add(item["provenance"])

        enriched.append(
            {
                "ingredient_id": item["ingredient_id"],
                "name": item["name"],
                "quantity": round(quantity, 2),
                "unit": item["unit"],
                "category": category,
                "provenance": item["provenance"],
                "supplier": item["supplier"],
                "diets": item.get("diets", ""),
                "stock_now": round(stock, 2),
                "stock_after_order": round(stock - quantity, 2),
                "risk_low_stock": (stock - quantity) <= min_stock,
            }
        )

    return {
        "available": available,
        "carbon": round(carbon, 3),
        "food_cost": round(food_cost, 2),
        "menu_price": round(food_cost * 2.9, 2),
        "calories": int(round(calories)),
        "protein_mass": round(protein_mass, 1),
        "anti_waste": round(anti_waste, 3),
        "composition": enriched,
        "origins": sorted(origins),
    }


def matches_exclusions(template, exclusion_tokens):
    if not exclusion_tokens:
        return False
    joined = " ".join(item["name"] for item in template.get("composition", []))
    normalized = normalize_text(joined)
    for token in exclusion_tokens:
        if token and token in normalized:
            return True
    return False


def build_stock_alerts(composition):
    alerts = []
    for item in composition:
        stock_now = float(item.get("stock_now", 0))
        stock_after = float(item.get("stock_after_order", 0))
        quantity = float(item.get("quantity", 0))
        if stock_now < quantity:
            alerts.append(
                {
                    "level": "critical",
                    "ingredient_id": item.get("ingredient_id"),
                    "ingredient_name": item.get("name"),
                    "message": "Stock insuffisant pour cette commande.",
                    "required": round(quantity, 2),
                    "available": round(stock_now, 2),
                    "unit": item.get("unit"),
                }
            )
        elif item.get("risk_low_stock"):
            alerts.append(
                {
                    "level": "warning",
                    "ingredient_id": item.get("ingredient_id"),
                    "ingredient_name": item.get("name"),
                    "message": "Le stock passe proche du seuil minimum.",
                    "remaining_after_order": round(stock_after, 2),
                    "unit": item.get("unit"),
                }
            )
    return alerts


def suggest_replacements(composition_item, ingredients_pool, requested_diet, max_suggestions=3):
    category = normalize_text(composition_item.get("category", ""))
    target_id = int(composition_item.get("ingredient_id", 0))
    requested = normalize_diet(requested_diet)
    candidates = []
    for ingredient in ingredients_pool:
        if int(ingredient.get("id", 0)) == target_id:
            continue
        if normalize_text(ingredient.get("category", "")) != category:
            continue
        if not ingredient_allows_diet(requested, ingredient.get("diets", "")):
            continue
        stock = float(ingredient.get("stock", 0))
        min_stock = float(ingredient.get("min_stock", 0))
        if stock <= (min_stock * 1.4):
            continue
        candidates.append(ingredient)

    candidates.sort(key=lambda row: float(row.get("stock", 0)) - float(row.get("min_stock", 0)), reverse=True)
    return [
        {
            "ingredient_id": int(item.get("id", 0)),
            "name": item.get("name"),
            "supplier": item.get("supplier"),
            "provenance": item.get("provenance"),
            "stock": round(float(item.get("stock", 0)), 2),
            "unit": item.get("unit"),
        }
        for item in candidates[:max_suggestions]
    ]


def enrich_course_stock_advice(course_payload, ingredients_pool, requested_diet):
    composition = course_payload.get("composition", [])
    alerts = build_stock_alerts(composition)
    suggestions = []
    for item in composition:
        if not item.get("risk_low_stock"):
            continue
        alternatives = suggest_replacements(item, ingredients_pool, requested_diet, max_suggestions=2)
        if not alternatives:
            continue
        suggestions.append(
            {
                "ingredient_id": item.get("ingredient_id"),
                "ingredient_name": item.get("name"),
                "category": item.get("category"),
                "alternatives": alternatives,
            }
        )
    course_payload["stock_alerts"] = alerts
    course_payload["replacement_suggestions"] = suggestions


def summarize_menu_stock(courses):
    critical = 0
    warning = 0
    for course in courses:
        for alert in course.get("stock_alerts", []):
            if alert.get("level") == "critical":
                critical += 1
            elif alert.get("level") == "warning":
                warning += 1
    if critical > 0:
        level = "critical"
    elif warning > 0:
        level = "warning"
    else:
        level = "ok"
    return {
        "level": level,
        "critical_alerts": critical,
        "warning_alerts": warning,
        "has_replacements": any(course.get("replacement_suggestions") for course in courses),
    }


def score_template(template, metrics, prefs):
    style = normalize_text(prefs.get("style", "surprise"))
    goal = normalize_choice_key(prefs.get("goal", "anti_waste"))
    budget = float(prefs.get("budget_max") or 0)

    score = 25.0
    reasons = []

    if style and style != "surprise":
        if normalize_text(template.get("style", "")) == style:
            score += 16
            reasons.append("style respecté")
        else:
            score -= 4

    if goal == "low_carbon":
        boost = max(0.0, 8.0 - (metrics["carbon"] * 2.4))
        score += boost
        reasons.append("empreinte carbone optimisée")
    elif goal == "high_protein":
        boost = (metrics["protein_mass"] / 25.0) - metrics["carbon"]
        score += boost
        reasons.append("profil riche en protéines")
    elif goal == "comfort":
        if template.get("style") in {"terroir", "classique"}:
            score += 8
            reasons.append("profil réconfort")
        score += max(0.0, 6.5 - (metrics["menu_price"] / 5.0))
    else:
        score += metrics["anti_waste"] * 2.5
        reasons.append("anti-gaspillage priorisé")

    if budget > 0:
        if metrics["menu_price"] <= budget:
            score += 10
            reasons.append("dans le budget")
        else:
            score -= (metrics["menu_price"] - budget) * 2.5

    score += random.uniform(-1.2, 1.2)

    return score, reasons


def build_recommendation(template, metrics, score, reasons, party_size):
    provenance_sentence = ", ".join(metrics["origins"][:4])
    history = template.get("story", "")
    base_price = float(template.get("base_price_eur") or 0)
    if base_price <= 0:
        base_price = float(metrics["menu_price"])

    return {
        "dish_id": template["id"],
        "name": template["name"],
        "style": template["style"],
        "diet": template["diet"],
        "description": (
            f"Assemblage calibré pour {party_size} personne(s), avec des ingrédients en stock réel."
        ),
        "history": history,
        "carbon_footprint_kg": metrics["carbon"],
        "price_estimate_eur": round(base_price, 2),
        "food_cost_eur": metrics["food_cost"],
        "calories_estimate": metrics["calories"],
        "protein_mass_g": metrics["protein_mass"],
        "provenance": metrics["origins"],
        "provenance_summary": f"Produits issus majoritairement de: {provenance_sentence}",
        "image_url": template["image_url"],
        "composition": metrics["composition"],
        "ai_score": round(score, 2),
        "selection_reasons": reasons,
    }


def choose_dish(payload):
    party_size = int(payload.get("party_size") or 1)
    if party_size < 1:
        party_size = 1
    if party_size > 12:
        party_size = 12

    catalog = load_catalog(force=False)
    ingredients_pool = load_ingredients(force=False)
    pricing_rules = load_pricing(force=False)
    requested_diet = normalize_diet(payload.get("diet", "omnivore"))
    style = normalize_text(payload.get("style", "surprise"))

    menu_type = normalize_choice_key(payload.get("menu_type", "decouverte")) or "decouverte"
    if menu_type not in {"decouverte", "signature", "single"}:
        menu_type = "decouverte"

    discovery_pair = normalize_choice_key(
        payload.get("discovery_pair")
        or payload.get("menu_variant")
        or payload.get("menu_option")
        or "entree_main"
    )
    if discovery_pair not in DISCOVERY_OPTIONS:
        discovery_pair = "entree_main"

    single_course = normalize_choice_key(payload.get("single_course", "main")) or "main"
    if single_course not in COURSE_LABELS:
        single_course = "main"

    if menu_type == "single":
        menu_pairing = f"{COURSE_LABELS[single_course]} seul"
        requested_courses = [single_course]
        menu_label = f"{COURSE_LABELS[single_course]} à la carte"
        menu_price = pricing_rules.get(f"single_{single_course}", SINGLE_PRICES_DEFAULT[single_course])
    elif menu_type == "signature":
        menu_pairing = "Entrée + Plat + Dessert"
        requested_courses = ["entree", "main", "dessert"]
        menu_label = MENU_LABELS["signature"]
        menu_price = pricing_rules.get("menu_signature", MENU_PRICES_DEFAULT["signature"])
    else:
        menu_pairing, requested_courses = DISCOVERY_OPTIONS[discovery_pair]
        menu_label = MENU_LABELS["decouverte"]
        menu_price = pricing_rules.get("menu_decouverte", MENU_PRICES_DEFAULT["decouverte"])

    excluded = payload.get("excluded_ingredients", [])
    if isinstance(excluded, str):
        excluded = [x.strip() for x in excluded.split(",") if x.strip()]
    exclusion_tokens = [normalize_text(x) for x in excluded if normalize_text(x)]

    rejected_ids = payload.get("rejected_ids", [])
    if not isinstance(rejected_ids, list):
        rejected_ids = []
    rejected = {int(x) for x in rejected_ids if str(x).isdigit()}

    ranked_by_course = {"entree": [], "main": [], "dessert": []}
    for template in catalog:
        template_id = int(template.get("id", 0))
        if template_id in rejected:
            continue

        course = normalize_text(template.get("course", "main")) or "main"
        if course not in ranked_by_course:
            course = "main"

        if not template_diet_consistent(template, requested_diet):
            continue

        if style and style != "surprise" and normalize_text(template.get("style", "")) != style:
            continue

        if matches_exclusions(template, exclusion_tokens):
            continue

        metrics = compute_metrics(template, party_size)
        if not metrics["available"]:
            continue

        score, reasons = score_template(template, metrics, payload)
        if course == "main":
            score += 4.0
        ranked_by_course[course].append((score, template, metrics, reasons))

    missing_courses = [course for course in requested_courses if not ranked_by_course.get(course)]
    if missing_courses:
        labels = [COURSE_LABELS.get(course, course) for course in missing_courses]
        raise ValueError(
            f"Aucun résultat disponible pour: {', '.join(labels)}. Retire des exclusions ou change de style."
        )

    selected_courses = []
    for course in requested_courses:
        ranked = ranked_by_course[course]
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        top_window = ranked[: min(8, len(ranked))]
        score, template, metrics, reasons = random.choice(top_window)
        course_payload = build_recommendation(template, metrics, score, reasons, party_size)
        course_payload["course"] = course
        course_payload["course_label"] = COURSE_LABELS.get(course, course.capitalize())
        course_payload["description"] = (
            f"{course_payload['course_label']} calibré(e) pour {party_size} personne(s), "
            "avec disponibilité confirmée sur le stock en direct."
        )
        enrich_course_stock_advice(course_payload, ingredients_pool, requested_diet)
        selected_courses.append(course_payload)

    main_course = next(
        (entry for entry in selected_courses if entry.get("course") == "main"),
        selected_courses[0],
    )
    generated_copy = None
    if main_course.get("course") == "main":
        generated_copy = openai_generate_copy(main_course, payload)
    if generated_copy:
        main_course["description"] = generated_copy["description"]
        main_course["history"] = generated_copy["story"]
        main_course["service_pitch"] = generated_copy["service_pitch"]
        main_course["plating_notes"] = generated_copy["plating_notes"]
        main_course["chef_note"] = generated_copy["chef_note"]
        main_course["natural_language_engine"] = f"openai:{OPENAI_MODEL}"
    else:
        main_course["service_pitch"] = (
            f"Suggestion {main_course['style']} prête à être servie, prix estimé {main_course['price_estimate_eur']}€."
        )
        main_course["plating_notes"] = "Dressage net, volume modéré et finition minute."
        main_course["chef_note"] = "Recette ajustée selon le stock réel du jour."
        main_course["natural_language_engine"] = "rules:fallback"

    alternatives = []
    alternatives_course = "main" if main_course.get("course") == "main" else main_course.get("course")
    for entry in sorted(ranked_by_course[alternatives_course], key=lambda row: row[0], reverse=True):
        alt_template = entry[1]
        if alt_template["id"] == main_course["dish_id"]:
            continue
        alternatives.append(
            {
                "dish_id": alt_template["id"],
                "name": alt_template["name"],
                "style": alt_template["style"],
                "carbon": round(entry[2]["carbon"], 2),
                "price": round(entry[2]["menu_price"], 2),
            }
        )
        if len(alternatives) >= 3:
            break

    raw_price = round(sum(float(item["price_estimate_eur"]) for item in selected_courses), 2)
    raw_food_cost = round(sum(item["food_cost_eur"] for item in selected_courses), 2)
    total_carbon = round(sum(item["carbon_footprint_kg"] for item in selected_courses), 3)
    stock_intelligence = summarize_menu_stock(selected_courses)
    menu_copy_payload = {
        "label": menu_label,
        "pairing": menu_pairing,
        "courses": [
            {
                "course": item.get("course"),
                "name": item.get("name"),
                "style": item.get("style"),
                "provenance": item.get("provenance", []),
            }
            for item in selected_courses
        ],
    }
    menu_copy = openai_generate_menu_copy(menu_copy_payload, payload)
    if not menu_copy:
        menu_copy = build_menu_fallback_copy(
            menu_label=menu_label,
            menu_pairing=menu_pairing,
            selected_courses=selected_courses,
            goal=payload.get("goal", "anti_waste"),
        )
        menu_copy_engine = "rules:fallback"
    else:
        menu_copy_engine = f"openai:{OPENAI_MODEL}"

    return {
        "dish": main_course,
        "menu": {
            "type": menu_type,
            "label": menu_label,
            "pairing": menu_pairing,
            "price_eur": menu_price,
            "raw_price_estimate_eur": raw_price,
            "raw_food_cost_eur": raw_food_cost,
            "total_carbon_kg": total_carbon,
            "menu_headline": menu_copy["menu_headline"],
            "menu_story": menu_copy["menu_story"],
            "service_pitch": menu_copy["service_pitch"],
            "natural_language_engine": menu_copy_engine,
            "stock_intelligence": stock_intelligence,
            "courses": selected_courses,
        },
        "alternatives": alternatives,
        "meta": {
            "catalog_size": len(catalog),
            "matching_candidates": sum(len(values) for values in ranked_by_course.values()),
            "party_size": party_size,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
    }


def serve_static_file(handler, path):
    candidate = path.lstrip("/")
    if candidate in {"", "/"}:
        candidate = "indexe2.0.html"

    full_path = os.path.join(PUBLIC_DIR, candidate)
    full_path = os.path.abspath(full_path)

    if not full_path.startswith(os.path.abspath(PUBLIC_DIR)):
        handler.send_error(403)
        return

    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        handler.send_error(404)
        return

    ctype, _ = mimetypes.guess_type(full_path)
    ctype = ctype or "application/octet-stream"

    with open(full_path, "rb") as stream:
        content = stream.read()

    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(content)))
    for key, value in SECURITY_HEADERS.items():
        handler.send_header(key, value)
    if ctype.startswith("text/html"):
        handler.send_header("Cache-Control", "no-store, max-age=0")
    handler.end_headers()
    handler.wfile.write(content)


class AIServerHandler(BaseHTTPRequestHandler):
    server_version = "HistorIAAI/1.0"

    def do_OPTIONS(self):
        json_response(self, {"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/admin.html":
            if not require_admin_network(self):
                return

        if path.startswith("/api/admin/"):
            if not require_admin_network(self):
                return

        if path == "/api/health":
            status, inventory_health = inventory_request("/api/health")
            return json_response(
                self,
                {
                    "service": "ai",
                    "status": "ok",
                    "inventory_status": status,
                    "inventory": inventory_health,
                    "orders_db": ORDERS_DB_PATH,
                    "natural_language_engine": f"openai:{OPENAI_MODEL}" if OPENAI_API_KEY else "rules:fallback",
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            )

        if path == "/api/live-stock":
            status, payload = inventory_request("/api/stock/summary")
            return json_response(self, payload, status=status)

        if path == "/api/ingredients":
            status, payload = inventory_request("/api/ingredients")
            return json_response(self, payload, status=status)

        if path == "/api/pricing":
            status, payload = inventory_request("/api/pricing")
            return json_response(self, payload, status=status)

        if path == "/api/admin/session":
            return json_response(
                self,
                {
                    "status": "open",
                    "auth": "disabled",
                },
            )

        if path == "/api/admin/ingredients":
            status, payload = inventory_request("/api/ingredients")
            return json_response(self, payload, status=status)

        if path == "/api/admin/stock-summary":
            status, payload = inventory_request("/api/stock/summary")
            return json_response(self, payload, status=status)

        if path == "/api/admin/stock-insights":
            status, payload = inventory_request("/api/stock/insights")
            return json_response(self, payload, status=status)

        if path == "/api/admin/dishes":
            query = f"?{parsed.query}" if parsed.query else ""
            status, payload = inventory_request(f"/api/dishes/admin{query}")
            return json_response(self, payload, status=status)

        if path == "/api/admin/pricing":
            status, payload = inventory_request("/api/pricing")
            return json_response(self, payload, status=status)

        if path == "/api/admin/orders":
            service_date = parse_service_date(params.get("date", [""])[0] if params.get("date") else "")
            if not service_date:
                return json_response(self, {"error": "date invalide (YYYY-MM-DD)"}, status=400)
            rows = fetch_orders_for_date(service_date)
            total = round(sum(float(item.get("price_eur", 0)) for item in rows), 2)
            return json_response(
                self,
                {
                    "date": service_date,
                    "count": len(rows),
                    "total_revenue_eur": total,
                    "orders": rows,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
            )

        if path == "/api/admin/export/orders.csv":
            service_date = parse_service_date(params.get("date", [""])[0] if params.get("date") else "")
            if not service_date:
                return json_response(self, {"error": "date invalide (YYYY-MM-DD)"}, status=400)
            rows = fetch_orders_for_date(service_date)
            csv_payload = build_orders_csv(service_date, rows)
            return binary_response(
                self,
                csv_payload,
                "text/csv; charset=utf-8",
                filename=f"historia-orders-{service_date}.csv",
            )

        if path == "/api/admin/export/orders.pdf":
            service_date = parse_service_date(params.get("date", [""])[0] if params.get("date") else "")
            if not service_date:
                return json_response(self, {"error": "date invalide (YYYY-MM-DD)"}, status=400)
            rows = fetch_orders_for_date(service_date)
            pdf_payload = build_orders_pdf(service_date, rows)
            return binary_response(
                self,
                pdf_payload,
                "application/pdf",
                filename=f"historia-orders-{service_date}.pdf",
            )

        if path == "/api/admin/ops":
            inventory_status, inventory_payload = inventory_request("/api/ops/status")
            return json_response(
                self,
                {
                    "service": "ai",
                    "status": "ok",
                    "inventory_status": inventory_status,
                    "inventory": inventory_payload,
                    "orders_db": ORDERS_DB_PATH,
                    "admin_allowlist": ADMIN_ALLOWED_IPS,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                },
                status=200 if inventory_status == 200 else 207,
            )

        if path == "/api/catalog-metrics":
            status, payload = inventory_request("/api/metrics")
            return json_response(self, payload, status=status)

        if path == "/":
            return serve_static_file(self, "historia.html")

        if path == "/indexe2.0.html":
            return serve_static_file(self, "indexe2.0.html")

        return serve_static_file(self, path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        payload = read_json_body(self)

        if path.startswith("/api/admin/"):
            if not require_admin_network(self):
                return

        if payload is None:
            return json_response(self, {"error": "JSON invalide"}, status=400)

        if path == "/api/admin/login":
            return json_response(
                self,
                {
                    "status": "open",
                    "auth": "disabled",
                    "message": "Aucun mot de passe requis",
                },
            )

        if path == "/api/generate-dish":
            try:
                recommendation = choose_dish(payload)
                return json_response(self, recommendation)
            except ValueError as exc:
                return json_response(self, {"error": str(exc)}, status=409)
            except RuntimeError as exc:
                return json_response(self, {"error": str(exc)}, status=503)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": f"Erreur IA: {exc}"}, status=500)

        if path == "/api/order-dish":
            party_size = int(payload.get("party_size") or 1)
            if party_size < 1:
                party_size = 1
            if party_size > 12:
                party_size = 12

            dish_ids = payload.get("dish_ids")
            normalized_ids = []
            if isinstance(dish_ids, list):
                for value in dish_ids:
                    if isinstance(value, int) or str(value).isdigit():
                        normalized_ids.append(int(value))

            if not normalized_ids:
                dish_id = payload.get("dish_id")
                if not isinstance(dish_id, int) and not str(dish_id).isdigit():
                    return json_response(self, {"error": "dish_id ou dish_ids obligatoire"}, status=400)
                normalized_ids = [int(dish_id)]

            normalized_ids = list(dict.fromkeys(normalized_ids))

            try:
                catalog = load_catalog(force=True)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, {"error": f"Catalogue indisponible: {exc}"}, status=503)

            selected_templates = []
            for current_id in normalized_ids:
                template = next((item for item in catalog if item["id"] == current_id), None)
                if template is None:
                    return json_response(self, {"error": f"Plat introuvable: {current_id}"}, status=404)
                selected_templates.append(template)

            grouped_items = {}
            for template in selected_templates:
                for comp in template.get("composition", []):
                    ingredient_id = int(comp["ingredient_id"])
                    grouped_items.setdefault(ingredient_id, 0.0)
                    grouped_items[ingredient_id] += float(comp["quantity"]) * party_size

            items = [
                {
                    "ingredient_id": ingredient_id,
                    "quantity": round(quantity, 3),
                }
                for ingredient_id, quantity in grouped_items.items()
            ]

            status, result = inventory_request("/api/stock/consume", method="POST", payload={"items": items})
            if status != 200:
                return json_response(
                    self,
                    {
                        "error": result.get("error", "Commande refusée"),
                        "details": result,
                    },
                    status=status,
                )

            summary_status, summary = inventory_request("/api/stock/summary")
            if summary_status != 200:
                summary = {"warning": "Impossible de récupérer le résumé du stock"}

            table_number = str(payload.get("table_number", "") or "").strip()
            menu_label = str(payload.get("menu_label", "") or "").strip()
            pairing = str(payload.get("pairing", "") or "").strip()
            try:
                price_eur = float(payload.get("price_eur"))
            except (TypeError, ValueError):
                price_eur = 0.0

            if price_eur <= 0:
                price_map = load_pricing(force=False)
                if len(selected_templates) == 3:
                    price_eur = price_map.get("menu_signature", MENU_PRICES_DEFAULT["signature"])
                elif len(selected_templates) == 2:
                    price_eur = price_map.get("menu_decouverte", MENU_PRICES_DEFAULT["decouverte"])
                else:
                    course = str(selected_templates[0].get("course", "main"))
                    price_eur = price_map.get(f"single_{course}", SINGLE_PRICES_DEFAULT.get(course, 24.0))

            if not menu_label:
                if len(selected_templates) == 3:
                    menu_label = MENU_LABELS["signature"]
                elif len(selected_templates) == 2:
                    menu_label = MENU_LABELS["decouverte"]
                else:
                    course = str(selected_templates[0].get("course", "main"))
                    menu_label = f"{COURSE_LABELS.get(course, 'Plat')} à la carte"
            if not pairing:
                pairing = " + ".join(
                    COURSE_LABELS.get(str(item.get("course", "main")), "Plat")
                    for item in selected_templates
                )

            created_at = datetime.utcnow().isoformat() + "Z"
            ticket_no = make_ticket_no()
            order_log_warning = None
            try:
                log_order_for_exports(
                    ticket_no=ticket_no,
                    created_at=created_at,
                    table_number=table_number,
                    menu_label=menu_label,
                    pairing=pairing,
                    party_size=party_size,
                    price_eur=price_eur,
                    ordered_dishes=[
                        {
                            "dish_id": template["id"],
                            "name": template["name"],
                            "course": template.get("course", "main"),
                        }
                        for template in selected_templates
                    ],
                )
            except Exception as exc:  # noqa: BLE001
                order_log_warning = f"Journal commande indisponible: {exc}"

            return json_response(
                self,
                {
                    "status": "ordered",
                    "ticket_no": ticket_no,
                    "dish_ids": normalized_ids,
                    "ordered_dishes": [
                        {
                            "dish_id": template["id"],
                            "name": template["name"],
                            "course": template.get("course", "main"),
                        }
                        for template in selected_templates
                    ],
                    "table_number": table_number,
                    "menu_label": menu_label,
                    "pairing": pairing,
                    "price_eur": round(float(price_eur), 2),
                    "party_size": party_size,
                    "inventory_update": result,
                    "stock_summary": summary,
                    "order_log_warning": order_log_warning,
                    "timestamp": created_at,
                },
            )

        if path == "/api/admin/restock":
            status, result = inventory_request("/api/stock/restock", method="POST", payload=payload)
            return json_response(self, result, status=status)

        if path == "/api/admin/stock-set":
            status, result = inventory_request("/api/stock/set", method="POST", payload=payload)
            return json_response(self, result, status=status)

        if path == "/api/admin/ingredient-update":
            status, result = inventory_request("/api/ingredient/update", method="POST", payload=payload)
            return json_response(self, result, status=status)

        if path == "/api/admin/ingredient-create":
            status, result = inventory_request("/api/ingredient/create", method="POST", payload=payload)
            return json_response(self, result, status=status)

        if path == "/api/admin/dish-update":
            status, result = inventory_request("/api/dish/update", method="POST", payload=payload)
            if status == 200:
                CATALOG_CACHE["value"] = None
                CATALOG_CACHE["at"] = None
            return json_response(self, result, status=status)

        if path == "/api/admin/pricing-set":
            status, result = inventory_request("/api/pricing/set", method="POST", payload=payload)
            if status == 200:
                PRICING_CACHE["value"] = None
                PRICING_CACHE["at"] = None
            return json_response(self, result, status=status)

        return json_response(self, {"error": "Route introuvable"}, status=404)

    def log_message(self, format_str, *args):
        print(f"[ai] {self.address_string()} - {format_str % args}")


def main():
    os.makedirs(PUBLIC_DIR, exist_ok=True)
    init_orders_db()
    server = ThreadingHTTPServer((HOST, PORT), AIServerHandler)
    print(f"AI server running on http://{HOST}:{PORT}")
    print(f"Inventory target: {INVENTORY_URL}")
    print(f"Public dir: {PUBLIC_DIR}")
    print(f"Natural language: {'OpenAI ' + OPENAI_MODEL if OPENAI_API_KEY else 'rules fallback'}")
    print("Admin auth: disabled")
    print(f"Admin network allowlist: {ADMIN_ALLOWED_IPS or 'none'}")
    print(f"Orders DB: {ORDERS_DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
