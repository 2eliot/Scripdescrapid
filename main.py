import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext

# ---------------------------------------------------------------------------
# Configuración de logs
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Globales — browser pre-lanzado, contexto fresco por request
# (reCAPTCHA v3 requiere página fresca; warm page causa tokens vencidos)
# ---------------------------------------------------------------------------
_playwright = None
_browser: Browser | None = None
_redeem_lock = asyncio.Lock()  # Serializar canjes (1 a la vez)

REDEEM_URL = "https://redeem.hype.games/"
TIMEOUT_MS = 30_000

# Keywords extraídas como constantes (evita recrear listas en cada request)
PIN_ERROR_KEYWORDS = [
    "already been redeemed", "already been used",
    "invalid pin", "pin inválido", "pin inv",
    "já foi utilizado", "pin not found",
    "código inválido", "invalid code",
    "pin ya fue", "ya fue canjeado",
    "not valid", "não é válido",
    "já foi resgatado", "expirado", "expired",
]

SUCCESS_KEYWORDS = [
    "successfully redeemed", "canjeado con éxito",
    "resgatado com sucesso", "congratulations",
    "canjeo exitoso", "fue canjeado",
    "parabéns", "felicidades",
    "your order has been", "pedido foi",
]

FORM_KEYWORDS = [
    "nome completo", "nombre completo", "full name",
    "gameaccountid", "id do jogador", "id de usuario",
]

STILL_ON_FORM_KEYWORDS = [
    "editar dados", "editar datos", "edit data",
    "canjear ahora", "resgatar agora", "redeem now",
    "insira seu pin", "ingrese su pin",
]

CONFIRM_ERROR_KEYWORDS = [
    "error", "erro", "failed", "invalid", "expired",
    "falhou", "falló", "tente novamente", "try again",
]

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-translate",
    "--disable-component-update",
    "--no-first-run",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--js-flags=--max-old-space-size=256",
]

# Tipos de recurso a bloquear (ahorra ancho de banda y RAM)
_BLOCKED_TYPES = frozenset(("image", "font", "media"))

# Dominios de rastreo que retrasan la carga sin aportar al flujo de reCAPTCHA
_BLOCKED_DOMAINS = (
    "google-analytics.com", "googletagmanager.com",
    "facebook.net", "facebook.com", "fbcdn.net",
    "hotjar.com", "doubleclick.net", "googlesyndication.com",
    "cloudflareinsights.com", "clarity.ms", "connect.facebook.net",
    "analytics.", "adservice.google",
)


async def _ensure_browser():
    """Garantiza que el browser esté vivo. Lo reinicia si crasheó."""
    global _playwright, _browser
    try:
        if _browser and _browser.is_connected():
            return
    except Exception:
        pass
    logger.warning("Browser caído, reiniciando...")
    # Limpiar
    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    # Re-lanzar
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True, args=BROWSER_ARGS
    )
    logger.info("Browser reiniciado ✓")


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser, _redeem_lock
    _redeem_lock = asyncio.Lock()
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True, args=BROWSER_ARGS
    )
    logger.info("Navegador Chromium pre-lanzado y listo ✓")
    yield
    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    logger.info("Navegador cerrado.")


app = FastAPI(title="Hype Games - Canjeador de PIN", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------
class RedeemRequest(BaseModel):
    pin_key: str
    full_name: str
    birth_date: str
    player_id: str
    country: str


class RedeemResponse(BaseModel):
    success: bool
    message: str
    player_name: str | None = None
    details: str | None = None


# ---------------------------------------------------------------------------
# Automatización principal OPTIMIZADA (contexto fresco por request)
# Target: 3-4 s por canje (antes ~8 s)
# ---------------------------------------------------------------------------
async def automate_redeem(data: RedeemRequest) -> RedeemResponse:
    ctx: BrowserContext | None = None
    start = time.time()
    try:
        await _ensure_browser()
        ctx = await _browser.new_context(
            viewport={"width": 1024, "height": 600},
            locale="pt-BR",
        )

        # ── Bloqueo refinado: tipos + dominios de rastreo ─────────────
        async def _block_resources(route):
            req = route.request
            if req.resource_type in _BLOCKED_TYPES:
                await route.abort()
                return
            url = req.url
            for dom in _BLOCKED_DOMAINS:
                if dom in url:
                    await route.abort()
                    return
            await route.continue_()
        await ctx.route("**/*", _block_resources)
        page = await ctx.new_page()

        # ── 1. Navegar con "commit" + esperar selector del PIN ────────
        logger.info("Navegando a %s", REDEEM_URL)
        await page.goto(REDEEM_URL, wait_until="commit", timeout=TIMEOUT_MS)
        await page.wait_for_selector("#pininput", state="visible", timeout=TIMEOUT_MS)
        logger.info("Página lista en %.1fs", time.time() - start)

        # ── 2. Llenar PIN + dismiss cookies en paralelo via JS ────────
        await page.evaluate("""(pin) => {
            // Llenar PIN inmediatamente
            const inp = document.querySelector('#pininput');
            if (inp) {
                inp.value = pin;
                inp.dispatchEvent(new Event('input', {bubbles:true}));
                inp.dispatchEvent(new Event('change', {bubbles:true}));
            }
            // Dismiss cookies
            document.querySelectorAll(
                '[class*="cookie"],[class*="consent"],[id*="cookie"],[id*="consent"],' +
                '[class*="Cookie"],[class*="Consent"],.cc-window,.cc-banner,#onetrust-banner-sdk'
            ).forEach(el => el.remove());
            const btns = document.querySelectorAll('button, a.btn, a[role="button"]');
            for (const b of btns) {
                const t = b.textContent.trim().toLowerCase();
                if (['aceptar','accept','aceitar','accept all','aceptar todo'].includes(t))
                    { b.click(); break; }
            }
        }""", data.pin_key)

        # ── 3. Esperar btn-validate habilitado (reCAPTCHA carga en fondo) ──
        btn_validate = page.locator("#btn-validate")
        for _ in range(40):
            disabled = await btn_validate.get_attribute("disabled")
            if disabled is None:
                break
            await asyncio.sleep(0.1)

        # ── 4. Click Verificar (force) + interceptar /validate ────────
        logger.info("Click Verificar PIN...")
        try:
            async with page.expect_response(
                lambda r: "/validate" in r.url and "account" not in r.url,
                timeout=TIMEOUT_MS
            ) as resp_info:
                await btn_validate.click(force=True)
            validate_response = await resp_info.value
            logger.info("Respuesta /validate: HTTP %s", validate_response.status)
            if validate_response.status >= 400:
                body = await validate_response.text()
                logger.warning("Error en /validate: %s", body[:300])
        except Exception as e:
            logger.warning("No se interceptó /validate: %s", e)

        # ── 5. Esperar formulario (#GameAccountId en DOM, no animación CSS) ──
        try:
            await page.wait_for_selector("#GameAccountId", state="attached", timeout=10_000)
        except Exception:
            # Fallback: esperar .card.back visible
            try:
                await page.locator(".card.back").wait_for(state="visible", timeout=5_000)
            except Exception:
                await asyncio.sleep(0.5)

        # ── 6. Verificar errores de PIN ───────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()

        for kw in PIN_ERROR_KEYWORDS:
            if kw.lower() in lower_text:
                logger.warning("Error de PIN: %s", kw)
                return RedeemResponse(
                    success=False,
                    message="Error de PIN",
                    details=f"El sitio devolvió un error: '{kw}'",
                )

        # Verificar que el formulario apareció
        card_back = page.locator(".card.back")
        card_back_html = ""
        if await card_back.count() > 0:
            card_back_html = await card_back.inner_html()
        if not card_back_html or "GameAccountId" not in card_back_html:
            if not any(kw in lower_text for kw in FORM_KEYWORDS):
                return RedeemResponse(
                    success=False,
                    message="Formulario no apareció después de validar PIN",
                    details=page_text[:500].strip(),
                )
        logger.info("Formulario detectado en %.1fs", time.time() - start)

        # ── 7. INYECCIÓN MASIVA: campos + país + habilitar botones ────
        country_name = data.country.lower()
        fill_result = await page.evaluate("""(args) => {
            const {name, born, playerId, country} = args;
            const r = {fields: false, country: false};

            // Campos de texto con dispatchEvent
            const nameEl = document.querySelector('#Name');
            const bornEl = document.querySelector('#BornAt');
            const idEl   = document.querySelector('#GameAccountId');
            const ev = (el, v) => {
                if (!el) return;
                el.value = v;
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            };
            ev(nameEl, name); ev(bornEl, born); ev(idEl, playerId);
            r.fields = !!(nameEl && bornEl && idEl);

            // Seleccionar país por texto del <option>
            const sel = document.querySelector('#NationalityAlphaCode');
            if (sel && sel.options.length > 1) {
                for (const opt of sel.options) {
                    if (opt.text.toLowerCase().includes(country)) {
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {bubbles:true}));
                        r.country = true; break;
                    }
                }
                if (!r.country) {
                    // Fallback: primera opción no vacía
                    for (const opt of sel.options) {
                        if (opt.value) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {bubbles:true}));
                            r.country = true; break;
                        }
                    }
                }
            }

            // Resetear checkboxes para que Playwright pueda marcarlos
            document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                cb.checked = false;
            });

            // Habilitar botones que el framework deja disabled
            document.querySelectorAll(
                '#btn-verify, #btn-verify-account, .btn-verify, #btn-redeem'
            ).forEach(b => b.removeAttribute('disabled'));

            // Limpiar overlays/modales que bloqueen clicks
            document.querySelectorAll(
                '[class*="overlay"],[class*="backdrop"],[class*="modal"]'
            ).forEach(el => {
                if (el.id !== 'btn-redeem' && !el.closest('.card')) el.remove();
            });

            return r;
        }""", {"name": data.full_name, "born": data.birth_date,
               "playerId": data.player_id, "country": country_name})
        logger.info("Fill masivo: %s", fill_result)

        # Si país no se cargó aún, esperar opciones async y seleccionar trusted
        if not fill_result.get("country"):
            country_sel = page.locator("#NationalityAlphaCode").first
            for _ in range(15):
                opt_count = await country_sel.evaluate("el => el.options.length")
                if opt_count > 1:
                    break
                await asyncio.sleep(0.1)
            try:
                target_value = await page.evaluate("""(cn) => {
                    const el = document.querySelector('#NationalityAlphaCode');
                    if (!el) return null;
                    for (const opt of el.options) {
                        if (opt.text.toLowerCase().includes(cn)) return opt.value;
                    }
                    for (const opt of el.options) { if (opt.value) return opt.value; }
                    return null;
                }""", country_name)
                if target_value:
                    await country_sel.select_option(value=target_value)
                    logger.info("País seleccionado (fallback trusted): %s", target_value)
            except Exception as e:
                logger.warning("Fallback país falló: %s", e)

        # ── 8. Checkboxes con click(force=True) — trusted pero sin esperar animaciones ──
        all_checkboxes = page.locator('input[type="checkbox"]')
        cb_count = await all_checkboxes.count()
        for i in range(cb_count):
            cb = all_checkboxes.nth(i)
            try:
                await cb.click(force=True, timeout=2000)
            except Exception:
                try:
                    cb_id = await cb.get_attribute("id") or f"idx{i}"
                    label = page.locator(f'label[for="{cb_id}"]')
                    if await label.count() > 0:
                        await label.click(force=True, timeout=2000)
                    else:
                        await cb.evaluate("el => { el.click(); }")
                except Exception as e:
                    logger.warning("Checkbox %d falló: %s", i, e)

        # ── 9. Click Verificar ID (force) + interceptar /validate/account ──
        player_name = None
        verify_btn = page.locator(
            '#btn-verify, #btn-verify-account'
        ).first
        if await verify_btn.count() > 0:
            logger.info("Click Verificar ID...")
            try:
                async with page.expect_response(
                    lambda r: "validate/account" in r.url, timeout=TIMEOUT_MS
                ) as response_info:
                    await verify_btn.click(force=True, timeout=3000)
                resp = await response_info.value
                resp_json = await resp.json()
                logger.info("validate/account: %s", resp_json)
                if resp_json.get("Success"):
                    player_name = resp_json.get("Username", "")
                else:
                    error_msg = resp_json.get("Message", "ID inválido")
                    logger.warning("Error de ID: %s", error_msg)
                    return RedeemResponse(
                        success=False,
                        message="Error de ID del jugador",
                        details=error_msg,
                    )
            except Exception as e:
                logger.warning("Verificar ID falló: %s", e)
        else:
            logger.warning("Botón Verificar ID no encontrado, continuando...")

        # ── 10. Submit canje final ────────────────────────────────────
        # Habilitar btn-redeem (puede haberse re-deshabilitado)
        await page.evaluate("""() => {
            const btn = document.querySelector('#btn-redeem');
            if (btn) btn.removeAttribute('disabled');
        }""")

        url_before = page.url
        confirm_ok = False
        confirm_body = ""

        # === Intento 1: Click Playwright force=True en #btn-redeem ===
        redeem_btn = page.locator("#btn-redeem")
        if await redeem_btn.count() > 0:
            logger.info("Submit: click #btn-redeem (force)...")
            try:
                async with page.expect_response(
                    lambda r: "/confirm" in r.url,
                    timeout=10_000
                ) as confirm_info:
                    await redeem_btn.click(force=True, timeout=3_000)
                confirm_resp = await confirm_info.value
                logger.info("/confirm: HTTP %s", confirm_resp.status)
                try:
                    confirm_body = await confirm_resp.text()
                except Exception:
                    pass
                if confirm_resp.status < 400:
                    confirm_ok = True
            except Exception as e:
                logger.warning("Intento 1 falló: %s", e)

        # === Intento 2: reCAPTCHA token fresco + JS click ===
        if not confirm_ok and not confirm_body:
            recaptcha_diag = await page.evaluate("""() => {
                const diag = {hasExecute: !!(window.grecaptcha && window.grecaptcha.execute), sitekey: null};
                const el = document.querySelector('[data-sitekey]');
                if (el) { diag.sitekey = el.getAttribute('data-sitekey'); return diag; }
                const iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                for (const f of iframes) {
                    const m = f.src.match(/[?&]k=([^&]+)/);
                    if (m) { diag.sitekey = m[1]; return diag; }
                }
                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                for (const s of scripts) {
                    const m = s.src.match(/render=([^&]+)/);
                    if (m) { diag.sitekey = m[1]; return diag; }
                }
                try {
                    const cfg = window.___grecaptcha_cfg;
                    if (cfg && cfg.clients) {
                        for (const cid in cfg.clients) {
                            const json = JSON.stringify(cfg.clients[cid]);
                            const m = json.match(/6L[a-zA-Z0-9_-]{38,}/);
                            if (m) { diag.sitekey = m[0]; return diag; }
                        }
                    }
                } catch(e) {}
                const m = document.documentElement.innerHTML.match(/6L[a-zA-Z0-9_-]{38,}/);
                if (m) diag.sitekey = m[0];
                return diag;
            }""")
            sitekey = recaptcha_diag.get("sitekey")

            if sitekey and recaptcha_diag.get("hasExecute"):
                logger.info("Intento 2: token reCAPTCHA + JS click...")
                try:
                    async with page.expect_response(
                        lambda r: "/confirm" in r.url, timeout=15_000
                    ) as confirm_info:
                        await page.evaluate("""(sk) => {
                            return new Promise((resolve) => {
                                window.grecaptcha.execute(sk, {action: 'confirm'}).then(token => {
                                    let inp = document.querySelector('#g-recaptcha-response') ||
                                              document.querySelector('textarea[name="g-recaptcha-response"]');
                                    if (!inp) {
                                        document.querySelectorAll('textarea').forEach(t => {
                                            if (t.name && t.name.includes('recaptcha')) inp = t;
                                        });
                                    }
                                    if (inp) { inp.value = token; inp.innerHTML = token; }
                                    const btn = document.querySelector('#btn-redeem');
                                    if (btn) {
                                        btn.removeAttribute('disabled');
                                        btn.click();
                                        resolve('ok');
                                    } else { resolve('no_btn'); }
                                }).catch(err => resolve('err: ' + err.message));
                            });
                        }""", sitekey)
                    confirm_resp = await confirm_info.value
                    logger.info("/confirm (JS): HTTP %s", confirm_resp.status)
                    try:
                        confirm_body = await confirm_resp.text()
                    except Exception:
                        pass
                    if confirm_resp.status < 400:
                        confirm_ok = True
                except Exception as e:
                    logger.warning("Intento 2 falló: %s", e)
            else:
                logger.warning("Sin sitekey reCAPTCHA para intento 2")

        # === Intento 3: form.submit() directo ===
        if not confirm_ok and not confirm_body:
            logger.info("Intento 3: form.submit()...")
            try:
                async with page.expect_response(
                    lambda r: "/confirm" in r.url, timeout=15_000
                ) as confirm_info:
                    await page.evaluate("""() => {
                        const form = document.querySelector('form');
                        if (form) form.submit();
                    }""")
                confirm_resp = await confirm_info.value
                try:
                    confirm_body = await confirm_resp.text()
                except Exception:
                    pass
                if confirm_resp.status < 400:
                    confirm_ok = True
            except Exception as e:
                logger.warning("Intento 3 falló: %s", e)

        if not confirm_ok and not confirm_body:
            return RedeemResponse(
                success=False,
                message="No se pudo enviar el formulario de canje",
                player_name=player_name,
                details="Los 3 intentos de submit fallaron",
            )

        # ── 11. Verificar resultado final ─────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        combined_text = (lower_text + " " + confirm_body.lower()).strip()
        logger.info("Resultado (200c): %s", page_text[:200].replace("\n", " "))

        for kw in SUCCESS_KEYWORDS:
            if kw in combined_text:
                logger.info("Canje EXITOSO keyword='%s' jugador=%s en %.1fs", kw, player_name, time.time() - start)
                return RedeemResponse(
                    success=True,
                    message="PIN canjeado exitosamente",
                    player_name=player_name,
                    details=kw,
                )

        # Analizar body de /confirm (JSON o texto)
        if confirm_ok and confirm_body:
            confirm_json = None
            try:
                confirm_json = json.loads(confirm_body)
            except Exception:
                pass

            if confirm_json and isinstance(confirm_json, dict):
                if confirm_json.get("Success") is True:
                    logger.info("Canje EXITOSO (JSON Success=true) jugador=%s en %.1fs", player_name, time.time() - start)
                    return RedeemResponse(
                        success=True,
                        message="PIN canjeado exitosamente",
                        player_name=player_name,
                        details=f"confirm JSON: {confirm_body[:200]}",
                    )
                else:
                    err_msg = confirm_json.get("Message", confirm_body[:200])
                    return RedeemResponse(
                        success=False,
                        message=f"Error del servidor: {err_msg}",
                        player_name=player_name,
                        details=confirm_body[:300],
                    )
            else:
                confirm_lower = confirm_body.lower()
                if not any(e in confirm_lower for e in CONFIRM_ERROR_KEYWORDS):
                    logger.info("Canje EXITOSO (HTTP 200 sin errores) jugador=%s en %.1fs", player_name, time.time() - start)
                    return RedeemResponse(
                        success=True,
                        message="PIN canjeado exitosamente",
                        player_name=player_name,
                        details=f"confirm HTTP 200, body: {confirm_body[:200]}",
                    )

        # Formulario sigue visible → NO se canjeó
        if any(kw in lower_text for kw in STILL_ON_FORM_KEYWORDS):
            return RedeemResponse(
                success=False,
                message="Canje no completado: el formulario sigue visible",
                player_name=player_name,
                details=page_text[:400].strip(),
            )

        # Sin confirmación clara → FALLO
        snippet = page_text[:500].strip()
        logger.warning("Resultado incierto: %s", snippet[:200])
        return RedeemResponse(
            success=False,
            message="Resultado incierto – no se confirmó el canje",
            player_name=player_name,
            details=snippet,
        )

    except Exception as exc:
        logger.exception("Error de automatización")
        return RedeemResponse(
            success=False,
            message="Error de automatización",
            details=str(exc),
        )
    finally:
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        elapsed = time.time() - start
        logger.info("Canje completado en %.1fs", elapsed)


# ---------------------------------------------------------------------------
# Endpoint de la API
# ---------------------------------------------------------------------------
@app.post("/redeem", response_model=RedeemResponse)
async def redeem_pin(data: RedeemRequest):
    logger.info("Petición de canje recibida para player_id=%s", data.player_id)
    async with _redeem_lock:
        result = await automate_redeem(data)
    return result


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "browser_ready": _browser is not None and _browser.is_connected(),
    }


@app.get("/metrics")
async def metrics():
    """Endpoint para monitorear uso de recursos en el VPS."""
    import os
    import psutil
    proc = psutil.Process(os.getpid())
    mem = proc.memory_info()
    return {
        "rss_mb": round(mem.rss / 1024 / 1024, 1),
        "vms_mb": round(mem.vms / 1024 / 1024, 1),
        "cpu_percent": proc.cpu_percent(interval=0.1),
        "threads": proc.num_threads(),
        "browser_connected": _browser is not None and _browser.is_connected(),
    }


# ---------------------------------------------------------------------------
# Ejecutar con: python main.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, log_level="info")
