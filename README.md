# Grocery Tracker PH

Track and compare grocery prices (in Philippine Peso) across stores and markets
in the Philippines. Record what each store charges for a product, then see which
store is cheapest — including by **unit price**, so different package sizes can
be compared fairly.

## Features

- **Stores & products** — full create / search / edit / delete. Products live in
  a shared catalog; each store records its own price for a product.
- **Cheapest-store comparison** — search a product (by name, brand, or category)
  and see every store ranked cheapest-first, with the cheapest highlighted.
- **Unit prices** — give a product a size (e.g. `1.5 L`, `60 g`) and compare
  ₱/100 mL or ₱/100 g across packages of different sizes.
- **Price history** — prices are append-only, so each new observation is kept;
  the "current" price is simply the most recent one.
- **Searchable category dropdown** and cascade deletes (deleting a store removes
  its prices; deleting a product removes its prices everywhere).

## Tech stack

Python · Flask · Jinja2 templates · Bootstrap 5 · SQLite (via SQLAlchemy).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The SQLite database (`grocery.db`) is created automatically on first run and is
not tracked in git, so you start with an empty catalog.

## Running

**Local development** (localhost only, auto-reloads on code changes):

```powershell
.venv\Scripts\python.exe app.py
```

**On your local network** (reachable by other devices on the same Wi-Fi; single
process, no auto-reload — restart it to pick up changes):

```powershell
.venv\Scripts\python.exe serve.py
```

Both serve on port `5000`. Open <http://127.0.0.1:5000>. For network access,
other devices use `http://<your-LAN-IP>:5000` (you may need to allow inbound TCP
5000 through your firewall).

## Tests

```powershell
.venv\Scripts\python.exe -m pytest
```
