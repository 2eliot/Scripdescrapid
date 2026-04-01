# Hype Games PIN Redeemer API

Migración a Node.js con Fastify + Playwright, manteniendo la lógica operativa de la versión Python y trasladando sus optimizaciones principales.

## Qué conserva la migración

- Browser Chromium pre-lanzado y compartido.
- Contexto fresco por request para evitar contaminación de sesión y problemas con reCAPTCHA.
- Límite de concurrencia configurable con `MAX_CONCURRENT_REDEEMS`.
- Cache de idempotencia por `request_id`.
- Endpoints `POST /redeem`, `GET /health` y `GET /metrics`.
- Bloqueo de recursos pesados y dominios de tracking para mejorar tiempos de respuesta.
- Fallbacks de submit y verificación final equivalentes a la versión Python.

## Instalación

```bash
npm install
npx playwright install chromium
```

## Ejecución

```bash
npm start
```

Por defecto el servidor escucha en `0.0.0.0:5000`.

Variables de entorno disponibles:

- `PORT`: puerto HTTP. Default `5000`.
- `HOST`: host de escucha. Default `0.0.0.0`.
- `MAX_CONCURRENT_REDEEMS`: cantidad máxima de canjes simultáneos. Default `3`.

## Uso

### POST /redeem

```bash
curl -X POST http://localhost:5000/redeem \
  -H "Content-Type: application/json" \
  -d '{
    "pin_key": "XXXX-XXXX-XXXX",
    "full_name": "Juan Pérez",
    "birth_date": "15/03/1990",
    "player_id": "123456789",
    "country": "Argentina",
    "request_id": "pedido-001"
  }'
```

### GET /health

```bash
curl http://localhost:5000/health
```

### GET /metrics

```bash
curl http://localhost:5000/metrics
```

## Notas

- La única entrada soportada del servicio es `main.js`.
- Si el sitio cambia selectores, estructura o la estrategia anti-bot, habrá que ajustar la automatización.
