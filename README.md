# HistorIA - 2 serveurs (AI + Stock)

## Ce que cette version apporte

1. Filtrage regime strict (`omnivore`, `pescatarian`, `vegetarian`, `vegan`) avec validation ingredient par ingredient.
2. Images de plats plus robustes (fallback local SVG garanti).
3. Stock intelligent: alertes + suggestions de remplacement.
4. Admin complet:
   - edition ingredients,
   - edition plats (entree/plat/dessert),
   - activation/desactivation des plats,
   - gestion des prix (menus et a la carte).
5. Exports compta:
   - commandes du jour JSON,
   - export CSV,
   - export PDF.
6. Ops/deploiement:
   - endpoint monitoring,
   - scripts backup/healthcheck,
   - persistance commandes serveur (`ORDERS_DB_PATH`).

## Architecture

- `inventory_server.py` (port `4100`)
  - base ingredients + templates + pricing
  - APIs stock, catalog, insights

- `ai_server.py` (port `4200`)
  - generation menu
  - commandes + journal serveur
  - exports admin CSV/PDF
  - pages `indexe2.0.html` et `admin.html`

## Lancer en local

Terminal A:
```bash
cd "historia-2-serveurs"
python3 inventory_server.py
```

Terminal B:
```bash
cd "historia-2-serveurs"
python3 ai_server.py
```

URLs:
- Client: `http://127.0.0.1:4200/indexe2.0.html`
- Admin: `http://127.0.0.1:4200/admin.html`

## Redemarrer proprement les serveurs

```bash
lsof -nP -iTCP:4100 -sTCP:LISTEN
lsof -nP -iTCP:4200 -sTCP:LISTEN
kill <pid_inventory> <pid_ai>
cd "historia-2-serveurs"
python3 inventory_server.py
# autre terminal
python3 ai_server.py
```

## Variables importantes

### AI
- `AI_HOST` (defaut `0.0.0.0`)
- `AI_PORT` (defaut `4200`)
- `INVENTORY_URL` (defaut `http://127.0.0.1:4100`)
- `OPENAI_API_KEY` (optionnel)
- `OPENAI_MODEL` (defaut `gpt-4.1-mini`)
- `ADMIN_ALLOWED_IPS` (allowlist admin)
- `ORDERS_DB_PATH` (defaut `./historia_orders.db`)

### Inventory
- `INVENTORY_HOST` (defaut `0.0.0.0`)
- `INVENTORY_PORT` (defaut `4100`)
- `DB_ENGINE=sqlite|postgres`
- `DATABASE_URL` (si postgres)

## APIs admin cle

- `GET /api/admin/ingredients`
- `GET /api/admin/stock-summary`
- `GET /api/admin/stock-insights`
- `GET /api/admin/dishes`
- `POST /api/admin/dish-update`
- `GET /api/admin/pricing`
- `POST /api/admin/pricing-set`
- `GET /api/admin/orders?date=YYYY-MM-DD`
- `GET /api/admin/export/orders.csv?date=YYYY-MM-DD`
- `GET /api/admin/export/orders.pdf?date=YYYY-MM-DD`
- `GET /api/admin/ops`

## Tests rapides

```bash
python3 -m unittest tests/test_diet_filters.py
./scripts/check_health.sh
```

## Backup

SQLite:
```bash
./scripts/backup_sqlite.sh ./historia_inventory.db ./backups
```

PostgreSQL:
```bash
DATABASE_URL='postgresql://...' ./scripts/backup_postgres.sh ./backups
```

## Deploiement

Voir: `DEPLOYMENT.md`
