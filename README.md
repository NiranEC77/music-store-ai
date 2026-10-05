# Music Store

A demo music store split into small services: the shop, the cart, orders, admin login, a traffic generator, and a chat assistant called the Metal Oracle.

This repository holds the store, cart, orders, users, database init, and the Metal Oracle chat service. The traffic generator still runs only from its published image. The store streams chat to the Oracle. It does not answer itself.

## Services

| Service | Port | What it does |
|---|---|---|
| Store | 5000 | Shop pages, album admin, and the Metal Oracle button |
| Order | 5001 | Creates orders and shows the order dashboard |
| Cart | 5002 | Cart, quantities, checkout, and a fake card payment |
| Users | 5003 | Admin login. The store asks it before opening the admin page |
| Traffic generator | 5004 | Pretends to be shoppers, using a browser |
| Chat | 5005 | Metal Oracle. A normal chat against the private model |
| PostgreSQL | 5432 | Albums. Database name `music_store` |

Cart and orders keep their own SQLite files. Users keeps `users.db`.

## Metal Oracle

The shop page has a button labeled with the horns hand. It opens a chat. Suggested questions cover thrash, death metal versus black metal, Metallica versus Pantera, and albums for an Iron Maiden fan.

The browser sends the question to the store. The store does not answer it. It forwards the same JSON to the chat service and streams the reply back. The chat service waits up to 150 seconds.

```
Browser  --POST /api/chat/stream-->  Store :5000  --POST /api/chat-->  Chat :5005
```

Request body:

```json
{"message": "Recommend me some thrash metal", "history": []}
```

`history` is the earlier turns, each `{ "role": "user" or "assistant", "content": "..." }`.

The reply is a server-sent event stream. Each piece is one of:

```
data: {"delta": "the answer"}
data: {"error": "what went wrong"}
data: [DONE]
```

Set `CHAT_SERVICE_URL` on the store. The default is `http://localhost:5005`. `chat-service/` is a normal chat. Before it calls the model it reads the album catalog and recent orders and puts that text in the prompt. It does not call tools.

`POST /mcp` on the same process still lists `album_count`, `list_albums`, and `list_orders`. The chat route does not call it.

The chat calls an OpenAI-compatible model (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`). On Tanzu, a GenAI service binding in `VCAP_SERVICES` supplies those three when the environment variables are empty. `OPENAI_TLS_VERIFY` defaults to on. Set it to `false` only when the private endpoint certificate does not match the name. With no model configured, the reply says the Oracle needs the private language model and does not read the catalog. The model endpoint and key belong in the `metal-oracle-model` secret (`base-url`, `api-key`, `model`), not in git.

`Dockerfile.chat-overlay` puts this `app.py` on top of the published store image `ghcr.io/niranec77/metal-music-store-store:1.0.63`. Build that image when you want the Oracle in a cluster, and set `CHAT_SERVICE_URL` on the store container.

## Admin login

The store has an Admin button. It posts to `/api/login`, `/api/logout`, and `/api/verify`, and the users service is what actually checks the password. The admin page stays closed unless the token belongs to an admin. Shoppers do not get accounts. The demo admin is `admin` / `metal`. Payments are still fake.

## What is in this repository

```
app.py
requirements.txt
Dockerfile.chat-overlay
docker-compose.yml
kustomization.yml
k8s-deployment.yaml
k8s-cart-deployment.yaml
k8s-order-deployment.yaml
k8s-users-deployment.yaml
k8s-database-deployment.yaml
k8s-traffic-generator-deployment.yaml
k8s-chat-deployment.yaml
chat-service/
cart-service/
order-service/
users-service/
database-service/init.sql
```

`docker-compose.yml` builds `./cart-service`, `./order-service`, `./users-service`, and `./chat-service`, and it mounts `./database-service/init.sql`. Those are in this repository. It also builds `./traffic-generator`, which is not. `docker compose up --build` still stops on the traffic generator until that directory exists. The shop, cart, orders, admin login, and the Oracle do not need it.

The Kubernetes files still pull the published images below. Build the Dockerfiles in this repository when that registry is not reachable. `Dockerfile.chat-overlay` puts this `app.py` on top of the published store image.

The Kubernetes files pull images that are already published:

- `ghcr.io/niranec77/metal-music-store-store:1.0.63`
- `ghcr.io/niranec77/metal-music-store-cart:1.0.63`
- `ghcr.io/niranec77/metal-music-store-order:1.0.63`
- `ghcr.io/niranec77/metal-music-store-users:1.0.63`
- `ghcr.io/niranec77/metal-music-store-database:1.0.63`
- `ghcr.io/niranec77/metal-music-store-traffic-generator:1.0.63`

The chat manifest is `k8s-chat-deployment.yaml`. `kubectl apply -k .` includes it.

## Run the published stack

```bash
kubectl apply -k .
kubectl apply -f k8s-traffic-generator-deployment.yaml
```

`kustomization.yml` includes the database, store, cart, orders, and users. The traffic generator is the extra file.

Ports on a laptop, when the services are running:

- Store: http://localhost:5000
- Orders: http://localhost:5001
- Cart: http://localhost:5002
- Users: http://localhost:5003
- Traffic generator: http://localhost:5004
- Chat, if you started one: http://localhost:5005
- Database: localhost:5432

## Store environment

| Variable | Default | Used for |
|---|---|---|
| `CART_SERVICE_URL` | `http://localhost:5002` | Cart |
| `ORDER_SERVICE_URL` | `http://localhost:5001` | Orders |
| `USERS_SERVICE_URL` | `http://localhost:5003` | Admin login |
| `CHAT_SERVICE_URL` | `http://localhost:5005` | Metal Oracle |
| `DB_HOST` | `localhost` | Postgres |
| `DB_PORT` | `5432` | Postgres |
| `DB_NAME` | `music_store` | Postgres |
| `DB_USER` | `music_user` | Postgres |
| `DB_PASSWORD` | `music_password` | Postgres |

The cart, order, and users services have their own URL and database-path variables in the Kubernetes files.

## Store routes

- `GET /` shop
- `GET /api/album/{id}` one album
- `POST /add_to_cart`, and the cart forwards for quantity, remove, and checkout
- `POST /api/chat/stream` Metal Oracle
- `POST /api/login`, `POST /api/logout`, `POST /api/verify` forwarded to users
- `GET /admin` admin page, after a valid admin token

Orders, from the order service:

- `POST /api/orders`
- `GET /api/orders`
- `GET /api/orders/{id}`
- `PUT /api/orders/{id}/status`

## Try the shop

1. Open the store and look through the albums.
2. Add one to the cart and check out. Any 13–19 digit card number, any future expiry, any 3–4 digit CVV.
3. Open the orders page and confirm the order is there.
4. Open the Metal Oracle only after a chat service is running. Ask one of the suggested questions and wait for the streamed reply.

## Notes

This is a demo. The card charge is not real. The database password in these files is a demo password. Do not use it for anything that matters.
