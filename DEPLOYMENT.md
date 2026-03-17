# Deploiement production - HistorIA

## 1) Pre-requis

- 2 services web:
  - `inventory_server.py`
  - `ai_server.py`
- Base distante PostgreSQL pour inventory.
- HTTPS actif (Render/Railway le font nativement, VPS via Nginx/Caddy).

## 2) Variables d'environnement

### Inventory
- `DB_ENGINE=postgres`
- `DATABASE_URL=postgresql://...`
- `INVENTORY_HOST=0.0.0.0`
- `INVENTORY_PORT=4100`

### AI
- `AI_HOST=0.0.0.0`
- `AI_PORT=4200`
- `INVENTORY_URL=https://<inventory-service>`
- `OPENAI_API_KEY=sk-...` (optionnel)
- `OPENAI_MODEL=gpt-4.1-mini`
- `ADMIN_ALLOWED_IPS=<ton_ip>/32,127.0.0.1,::1`
- `ORDERS_DB_PATH=/data/historia_orders.db` (ou `/tmp/...` si ephemere)

## 3) Render

1. Push du repo sur GitHub.
2. Créer un "Blueprint" via `render.yaml`.
3. Services crees:
   - `historia-inventory`
   - `historia-ai`
4. Renseigner les env vars non synchronisees (`DATABASE_URL`, `INVENTORY_URL`, `ADMIN_ALLOWED_IPS`, `OPENAI_API_KEY`).
5. Verifier:
   - `GET /api/health`
   - client: `/indexe2.0.html`
   - admin (depuis IP autorisee): `/admin.html`

## 4) Railway

1. Creer projet + PostgreSQL.
2. Service inventory (`python3 inventory_server.py`) + vars inventory.
3. Service ai (`python3 ai_server.py`) + vars ai.
4. Connecter `INVENTORY_URL` sur l'URL publique/private de inventory.

## 5) VPS Docker

```bash
cd historia-2-serveurs
docker compose up -d --build
```

- `postgres` persiste via volume `historia_pg_data`.
- `ai` persiste le journal commandes via volume `historia_ai_data` (`ORDERS_DB_PATH=/data/historia_orders.db`).

## 6) Monitoring

- Endpoint inventory: `GET /api/ops/status`
- Endpoint ai: `GET /api/admin/ops`
- Script local:
```bash
./scripts/check_health.sh
```

## 7) Backup

### PostgreSQL
```bash
DATABASE_URL='postgresql://...' ./scripts/backup_postgres.sh ./backups
```

### SQLite (si mode local)
```bash
./scripts/backup_sqlite.sh ./historia_inventory.db ./backups
```

## 8) Securite (sans login admin)

- Les routes admin sont limitees a `ADMIN_ALLOWED_IPS`.
- Ne jamais laisser `ADMIN_ALLOWED_IPS` vide en public.
- Exposer uniquement `ai` au web; `inventory` idealement en reseau prive inter-services.

## 9) Validation finale

1. Regime strict: `omnivore` ne doit plus renvoyer de `vegetarian/vegan`.
2. Admin plats/prix: edition fonctionnelle et prix visibles cote client.
3. Exports commandes du jour: JSON + CSV + PDF.
4. Stock intelligent: alertes + suggestions disponibles en admin/client.
