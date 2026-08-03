# Alert TTL (retention) — revision: `ttl_active` toggle

## Goal (revision of implemented feature)
Replace the "empty days = persistence" semantics with an explicit **`ttl_active` boolean**:
- Admin sets `ttl_active` to **false** → alerts persist forever (days value ignored).
- Admin sets `ttl_active` to **true** → `retention_days` must be set; alerts older than that are purged.

The rest of the pipeline (command, scheduling, cascade) is unchanged.

## Delta from the current implementation

### 1. Model — `telemetry/models.py` (`AlertRetentionPolicy`)
- Add `ttl_active = models.BooleanField(default=False)`.
- `retention_days` help text → "Delete alerts older than this many days. Required when TTL is active." (remove "leave empty to keep forever").
- Add `clean()` validation:
  - `ttl_active and not retention_days` → `ValidationError` ("Set a number of days when TTL is active.").
  - Otherwise `retention_days` value is kept but ignored when inactive (non-destructive).
- `__str__`: active+days → "TTL active: delete alerts older than N days"; else "TTL disabled: alerts are kept forever".
- `get_retention_days()` → returns `None` unless `obj.ttl_active and obj.retention_days`.

### 2. Migration
`python manage.py makemigrations telemetry` → `0014_alertretentionpolicy_ttl_active` (AddField `ttl_active`, default False). Existing local row becomes inactive → persistence. Safe default.

### 3. Admin — `telemetry/admin.py`
- New `AlertRetentionPolicyForm(ModelForm)`:
  - `clean()` → field-level error on `retention_days` when `ttl_active` is checked but days empty.
- `AlertRetentionPolicyAdmin`:
  - `form = AlertRetentionPolicyForm`
  - `fields`/`list_display` → `('ttl_active', 'retention_days', 'impact_preview', 'updated_at')`.
  - `retention_days` is always editable. (Deviation: a `get_readonly_fields` override that made
    `retention_days` readonly while inactive was tried and removed — a readonly field is excluded
    from the ModelForm, so the field-level validation error on the enable path crashed with
    `ValueError: has no field named 'retention_days'`, and an admin enabling TTL could never type
    days (deadlock). Always-editable avoids both.)
  - `impact_preview`:
    - not active → "TTL disabled: alerts are kept forever"
    - active with days → purge count (as today)
    - active, no days → "TTL active but no retention days set" (defensive).
- `has_add_permission` / `has_delete_permission` unchanged.

### 4. Command — `purge_alerts.py`
Unchanged (already reads `get_retention_days()`; returns None → persistence no-op).

### 5. Tests — `telemetry/tests.py`
- Keep command tests (they mock `get_retention_days`).
- Update/extend model-helper tests:
  - no policy → None
  - policy `ttl_active=False`, days=30 → None
  - policy `ttl_active=True`, days=30 → 30
  - policy `ttl_active=True`, days=None → None (defensive)
- Add `clean()` validation test: active without days raises `ValidationError`; inactive with days is valid.

### 6. Docs
- `README.md` "Alert retention (TTL)": describe the toggle (check TTL active + enter days; uncheck = keep forever).
- `WIS2 Monitoring Project.txt` §2.8: add `ttl_active`; §8.5: note the toggle semantics.

## Verification
1. `python manage.py check` (expect 5 pre-existing W042)
2. `python manage.py test telemetry` (57 tests, 4 pre-existing errors)
3. `python manage.py makemigrations --check --dry-run` clean
4. Apply `migrate`, then smoke:
   - Admin change form: checkbox present; days always editable; check + empty days → inline error;
     check + days → impact preview shows count.
   - Command: `purge_alerts --dry-run` with `ttl_active=False` → persistence message; `ttl_active=True` days=30 → purge count.
   - Reset policy to default (ttl_active=False) after smoke.

## Deploy
`migrate` (0014) on server; existing policy row becomes inactive (persistence). Set the toggle in admin. Existing `wis2-purge.timer` keeps running nightly.
