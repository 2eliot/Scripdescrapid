# Hype Games PIN Redeemer API

API FastAPI + Playwright que automatiza el canje de PINs en https://redeem.hype.games/client/en-US/redeem

## Instalación en VPS

```bash
# 1. Instalar dependencias Python
pip install -r requirements.txt

# 2. Instalar navegador Chromium para Playwright
playwright install chromium
playwright install-deps   # solo en Linux, instala dependencias del sistema
```

## Ejecución

```bash
python main.py
```

El servidor escucha en `0.0.0.0:5000`.

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
    "country": "Argentina"
  }'
```

### Respuesta exitosa

```json
{
  "success": true,
  "message": "PIN redeemed successfully",
  "details": "successfully redeemed"
}
```

### Respuesta con error

```json
{
  "success": false,
  "message": "PIN error",
  "details": "The site returned an error related to: 'already been redeemed'"
}
```

### GET /health

Verifica que el servidor y el navegador estén listos.

```bash
curl http://localhost:5000/health
```

## Notas

- El navegador Chromium se pre-lanza al iniciar la app para respuestas rápidas (<5s).
- Cada petición usa un contexto aislado (cookies/sesión independientes).
- Los selectores CSS pueden necesitar ajuste si el sitio cambia su estructura HTML.
- Si el sitio usa reCAPTCHA activamente, podría bloquear la automatización headless.
