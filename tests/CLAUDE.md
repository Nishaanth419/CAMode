# tests/CLAUDE.md

Rules for writing tests. Strategy and evals: `docs/architecture/testing.md`.

- Tests never call live AI, MCP or OAuth services: mock them. One `test_<module>.py` per backend module; browser helpers in `tests/js/` (`node --test tests/js/*.test.js`).
- `conftest.py` gives every test a temporary `app.db`, a generated `APP_MASTER_KEY`, a lowered `users.SCRYPT_N`, no scheduler, and clears or sets env flags (`REQUEST_ROUTER=keywords`, `AGENT_GRAPH_ENABLED=false`, `MF_DATA_ENABLED=false`, `LOGOS_ENABLED=false`, no `LOGO_DEV_TOKEN`, temp `BACKUP_DIR`, limits and Turnstile cleared). Never touch `.data/`, the real master key or the real database.
- Fixtures: `client`, `signed_in` (signed-in, unlocked profile that accepted the notice in `device` mode), `sign_up()` (`storage_mode=None` skips consent, `"private"` for private mode); `google_mode` and `google_sign_in()` in `test_api.py`.
- Synthetic data in the real response layout lives in `tests/fixtures/` (`indmoney_sample.py`, `indmoney_funds.py`, `indmoney_market.py`, `kite_sample.py`, `mf_tax_sample.py`, `snapshot_pairs.py`), `tests/mf_fixtures.py` and `tests/login_tool_brokers.py` (login-tool broker variants with Kite's old `get_*` tools). Never put real values in them.
- Changed tax rules, routing or the agent loop: also run the scripted evals (`python -m scripts.eval_tasks`, `python -m scripts.eval_workflows`); `test_eval_*.py` run them in the suite.
- CI runs `python -m compileall -q app tests && python -m pytest -q`.
