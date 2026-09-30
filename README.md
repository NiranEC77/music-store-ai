# Music Store

A demo music store split into small services: the shop, the cart, orders, admin login, a traffic generator, and a chat assistant called the Metal Oracle.

This repository holds the store service source (`app.py`) and the deployment files. The cart, orders, users, database, and traffic generator run from published images. The chat brain is a separate service. The store only streams to it.

## Services

| Service | Port | What it does |
|---|---|---|
| Store | 5000 | Shop pages, album admin, and the Metal Oracle button |
| Order | 5001 | Creates orders and shows the order dashboard |
| Cart | 5002 | Cart, quantities, checkout, and a fake card payment |
| Users | 5003 | Admin login. The store asks it before opening the admin page |
| Traffic generator | 5004 | Pretends to be shoppers, using a browser |
| Chat | 5005 | Answers the Metal Oracle. Not started by the files in this repo |
| PostgreSQL | 5432 | Albums. Database name `music_store` |

Cart and orders keep their own SQLite files. Users keeps `users.db`.

## Metal Oracle

The shop page has a button labeled with the horns hand. It opens a chat. Suggested questions cover thrash, death metal versus black metal, Metallica versus Pantera, and albums for an Iron Maiden fan.

The browser sends the question to the store. The store does not answer it. It forwards the same JSON to the chat service and streams the reply back.

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
data: {"delta": "next few words"}
data: {"error": "what went wrong"}
data: [DONE]
```

The store waits up to 90 seconds, then tells the browser the chat service timed out.

Set `CHAT_SERVICE_URL` on the store. The default is `http://localhost:5005`. This repo does not contain the chat service, and `docker-compose.yml` does not start one. The Kubernetes store manifest does not set `CHAT_SERVICE_URL` either. Until a chat service is running at that address, the button opens and the reply says the Oracle is unreachable.

`Dockerfile.chat-overlay` puts this `app.py` on top of the published store image `ghcr.io/niranec77/metal-music-store-store:1.0.63`. Build that image when you want the Oracle in a cluster, and set `CHAT_SERVICE_URL` on the store container.

## Admin login

The store has an Admin button. It posts to `/api/login`, `/api/logout`, and `/api/verify`, and the users service is what actually checks the password. The admin page stays closed unless the token belongs to an admin. Shoppers do not get accounts. Payments are still fake.

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
```

`docker-compose.yml` builds `./cart-service`, `./order-service`, `./users-service`, and `./traffic-generator`, and it mounts `./database-service/init.sql`. Those directories are not in this repository. `docker compose up --build` from a fresh clone does not start the stack.

The Kubernetes files pull images that are already published:

- `ghcr.io/niranec77/metal-music-store-store:1.0.63`
- `ghcr.io/niranec77/metal-music-store-cart:1.0.63`
- `ghcr.io/niranec77/metal-music-store-order:1.0.63`
- `ghcr.io/niranec77/metal-music-store-users:1.0.63`
- `ghcr.io/niranec77/metal-music-store-database:1.0.63`
- `ghcr.io/niranec77/metal-music-store-traffic-generator:1.0.63`

There is no chat image in those manifests.

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
