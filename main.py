import asyncio
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

BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


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
# Automatización principal (contexto fresco por request)
# ---------------------------------------------------------------------------
async def automate_redeem(data: RedeemRequest) -> RedeemResponse:
    ctx: BrowserContext | None = None
    start = time.time()
    try:
        await _ensure_browser()
        ctx = await _browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="pt-BR",
        )
        page = await ctx.new_page()

        # ── 1. Navegar (networkidle para que reCAPTCHA v3 cargue) ──────
        logger.info("Navegando a %s", REDEEM_URL)
        await page.goto(REDEEM_URL, wait_until="networkidle", timeout=TIMEOUT_MS)
        await asyncio.sleep(2)  # Cloudflare Rocket Loader
        elapsed = time.time() - start
        logger.info("Página cargada en %.1fs", elapsed)

        # Esperar a que reCAPTCHA esté disponible
        recaptcha_ready = False
        for _ in range(20):
            recaptcha_ready = await page.evaluate(
                "() => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function'"
            )
            if recaptcha_ready:
                break
            await asyncio.sleep(0.5)
        logger.info("reCAPTCHA disponible: %s", recaptcha_ready)

        # ── 2. Ingresar el PIN ────────────────────────────────────────
        logger.info("Ingresando PIN...")
        pin_input = page.locator("#pininput")
        await pin_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        await pin_input.fill(data.pin_key)

        # Esperar a que el botón se habilite
        logger.info("Esperando que botón Verificar se habilite...")
        btn_validate = page.locator("#btn-validate")
        await btn_validate.wait_for(state="visible", timeout=TIMEOUT_MS)
        for _ in range(30):
            disabled = await btn_validate.get_attribute("disabled")
            if disabled is None:
                break
            await asyncio.sleep(0.2)

        # Clic en Verificar e interceptar la respuesta AJAX de /validate
        logger.info("Haciendo clic en Verificar (interceptando /validate)...")
        validate_response = None
        try:
            async with page.expect_response(
                lambda r: "/validate" in r.url and "account" not in r.url,
                timeout=TIMEOUT_MS
            ) as resp_info:
                await btn_validate.click()
            validate_response = await resp_info.value
            validate_status = validate_response.status
            logger.info("Respuesta /validate: HTTP %s", validate_status)
            if validate_status >= 400:
                body = await validate_response.text()
                logger.warning("Error en /validate: %s", body[:300])
        except Exception as e:
            logger.warning("No se pudo interceptar /validate: %s", e)
            # Continuar de todos modos

        # Esperar a que .card.back aparezca (el flip real del formulario)
        logger.info("Esperando flip de tarjeta (.card.back visible)...")
        try:
            await page.locator(".card.back").wait_for(state="visible", timeout=15_000)
        except Exception:
            await asyncio.sleep(2)

        # ── 3. Verificar errores de PIN ────────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        logger.info("Texto de página (200 chars): %s", page_text[:200].replace("\n", " "))

        pin_error_keywords = [
            "already been redeemed", "already been used",
            "invalid pin", "pin inválido", "pin inv",
            "já foi utilizado", "pin not found",
            "código inválido", "invalid code",
            "pin ya fue", "ya fue canjeado",
            "not valid", "não é válido",
            "já foi resgatado", "expirado", "expired",
        ]
        for kw in pin_error_keywords:
            if kw.lower() in lower_text:
                logger.warning("Error de PIN detectado: %s", kw)
                return RedeemResponse(
                    success=False,
                    message="Error de PIN",
                    details=f"El sitio devolvió un error: '{kw}'",
                )

        # Verificar que el formulario apareció en .card.back
        card_back = page.locator(".card.back")
        card_back_html = ""
        if await card_back.count() > 0:
            card_back_html = await card_back.inner_html()
        
        if not card_back_html or "GameAccountId" not in card_back_html:
            # Tal vez la página no hizo flip. Verificar con texto general
            form_keywords = ["nome completo", "nombre completo", "full name",
                             "gameaccountid", "id do jogador", "id de usuario"]
            if not any(kw in lower_text for kw in form_keywords):
                snippet = page_text[:500].strip()
                return RedeemResponse(
                    success=False,
                    message="Formulario no apareció después de validar PIN",
                    details=snippet,
                )

        logger.info("Formulario detectado en .card.back")

        # ── 4. Llenar formulario COMPLETO de golpe via JS (instantáneo) ──
        logger.info("Llenando formulario via JS instantáneo...")
        fill_ok = await page.evaluate("""(args) => {
            const {name, born, country, playerId} = args;

            // Nombre
            const nameEl = document.querySelector('#Name');
            if (nameEl) { nameEl.value = name; nameEl.dispatchEvent(new Event('input', {bubbles:true})); }

            // Fecha de nacimiento
            const bornEl = document.querySelector('#BornAt');
            if (bornEl) { bornEl.value = born; bornEl.dispatchEvent(new Event('input', {bubbles:true})); }

            // País — buscar por texto parcial en las opciones del select
            const selEl = document.querySelector('#NationalityAlphaCode');
            if (selEl) {
                const countryLower = country.toLowerCase();
                for (const opt of selEl.options) {
                    if (opt.text.toLowerCase().includes(countryLower)) {
                        selEl.value = opt.value;
                        selEl.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }

            // Player ID
            const idEl = document.querySelector('#GameAccountId');
            if (idEl) { idEl.value = playerId; idEl.dispatchEvent(new Event('input', {bubbles:true})); }

            // Checkbox privacidad
            const cb = document.querySelector('#privacy');
            if (cb && !cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }

            return !!(nameEl && bornEl && idEl);
        }""", {"name": data.full_name, "born": data.birth_date,
               "country": data.country, "playerId": data.player_id})
        logger.info("Formulario llenado via JS: %s (nombre=%s, id=%s)",
                     "OK" if fill_ok else "parcial", data.full_name, data.player_id)

        # ── 6. Clic en botón VERIFICAR ID ────────────────────────────
        # El JS llama a validate/account via AJAX que retorna {"Success":true,"Username":"NOMBRE"}
        # Interceptamos esa respuesta para obtener el player_name directamente
        player_name = None

        # Forzar habilitación del botón verify (el JS de la página lo deja disabled
        # si los eventos de validación no se dispararon correctamente con el fill via JS)
        await page.evaluate("""() => {
            const btns = document.querySelectorAll('#btn-verify, #btn-verify-account, .btn-verify');
            btns.forEach(b => b.removeAttribute('disabled'));
        }""")

        logger.info("Buscando botón de verificar ID...")
        verify_btn = page.locator(
            '#btn-verify,'
            'button:has-text("Verificar ID"),'
            'button:has-text("Verify ID"),'
            'button:has-text("Verificar Id"),'
            '#btn-verify-account'
        ).first
        if await verify_btn.count() > 0:
            logger.info("Haciendo clic en Verificar ID (interceptando respuesta AJAX)...")

            # Interceptar la respuesta de validate/account para obtener Username
            async with page.expect_response(
                lambda r: "validate/account" in r.url, timeout=TIMEOUT_MS
            ) as response_info:
                await verify_btn.click(timeout=5000)

            try:
                resp = await response_info.value
                resp_json = await resp.json()
                logger.info("Respuesta validate/account: %s", resp_json)

                if resp_json.get("Success"):
                    player_name = resp_json.get("Username", "")
                    logger.info("Player name desde AJAX: %s", player_name)
                else:
                    error_msg = resp_json.get("Message", "ID inválido")
                    logger.warning("Error de ID: %s", error_msg)
                    return RedeemResponse(
                        success=False,
                        message="Error de ID del jugador",
                        details=error_msg,
                    )
            except Exception as e:
                logger.warning("No se pudo parsear respuesta validate/account: %s", e)

            await asyncio.sleep(0.5)
        else:
            logger.warning("Botón Verificar ID no encontrado, continuando...")

        # ── 8. Marcar checkboxes con Playwright TRUSTED click ──────────
        # JS checkbox.checked=true NO activa los handlers del framework.
        # Necesitamos click real de Playwright para que el form acepte los términos.
        logger.info("Marcando checkboxes con Playwright (trusted click)...")
        all_checkboxes = page.locator('input[type="checkbox"]')
        cb_count = await all_checkboxes.count()
        for i in range(cb_count):
            cb = all_checkboxes.nth(i)
            try:
                if await cb.is_visible() and not await cb.is_checked():
                    await cb.click(timeout=3000)
                    logger.info("Checkbox %d marcado via Playwright", i)
            except Exception as e:
                logger.warning("Checkbox %d falló: %s", i, e)

        await asyncio.sleep(0.5)

        # ── 9. Clic en botón final de canje ──────────────────────────
        logger.info("Habilitando y buscando botón de canje final...")

        confirm_ok = False
        confirm_body = ""

        # Habilitar btn-redeem via JS
        await page.evaluate("""() => {
            const btn = document.querySelector('#btn-redeem');
            if (btn) btn.removeAttribute('disabled');
        }""")

        # Capturar URL antes del click para detectar navegación
        url_before = page.url

        # Click con Playwright en #btn-redeem (trusted click)
        redeem_btn = page.locator("#btn-redeem")

        if await redeem_btn.count() > 0 and await redeem_btn.is_visible():
            logger.info("Haciendo clic Playwright en #btn-redeem (trusted)...")
            try:
                # Esperar navegación O respuesta AJAX
                async with page.expect_response(
                    lambda r: "/confirm" in r.url,
                    timeout=15_000
                ) as confirm_info:
                    await redeem_btn.click(timeout=10_000)
                confirm_resp = await confirm_info.value
                logger.info("Respuesta /confirm: HTTP %s URL: %s", confirm_resp.status, confirm_resp.url)
                try:
                    confirm_body = await confirm_resp.text()
                    logger.info("Body /confirm (500 chars): %s", confirm_body[:500].replace("\n", " "))
                except Exception as te:
                    logger.warning("No se pudo leer body de /confirm: %s", te)
                if confirm_resp.status < 400:
                    confirm_ok = True
            except Exception as e:
                logger.warning("Click #btn-redeem o intercept /confirm falló: %s", e)
        else:
            logger.info("#btn-redeem no visible, buscando por texto...")
            keywords = ["Resgatar", "Canjear", "Redeem"]
            clicked = False
            for kw in keywords:
                btn = page.locator(f'button:visible:has-text("{kw}")').first
                if await btn.count() > 0:
                    await btn.evaluate("el => el.removeAttribute('disabled')")
                    logger.info("Haciendo clic Playwright en botón '%s'...", kw)
                    try:
                        await btn.click(timeout=10_000)
                        confirm_ok = True
                        clicked = True
                    except Exception as e:
                        logger.warning("Click en '%s' falló: %s", kw, e)
                    break
            if not clicked:
                return RedeemResponse(
                    success=False,
                    message="No se encontró botón de canje final visible",
                    details=page_text[:300],
                )

        # Esperar navegación o cambio de página
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Capturar URL actual
        url_after = page.url
        logger.info("URL antes: %s → después: %s", url_before, url_after)
        url_changed = url_after != url_before

        # ── 10. Verificar resultado final ─────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        logger.info("Resultado final (300 chars): %s", page_text[:300].replace("\n", " "))

        # Combinar texto de página + body de /confirm para buscar éxito
        combined_text = (lower_text + " " + confirm_body.lower()).strip()

        success_keywords = [
            "successfully redeemed", "canjeado con éxito",
            "resgatado com sucesso", "congratulations",
            "canjeo exitoso", "fue canjeado",
            "parabéns", "felicidades",
            "your order has been", "pedido foi",
        ]
        for kw in success_keywords:
            if kw in combined_text:
                logger.info("¡Canje exitoso confirmado! keyword='%s' Jugador: %s", kw, player_name)
                return RedeemResponse(
                    success=True,
                    message="PIN canjeado exitosamente",
                    player_name=player_name,
                    details=kw,
                )

        # Si /confirm respondió 200, verificar su body para errores
        if confirm_ok and confirm_body:
            confirm_lower = confirm_body.lower()
            # Si el body de /confirm contiene error claro
            error_in_confirm = any(e in confirm_lower for e in [
                "error", "failed", "invalid", "expired", "falhou", "falló"
            ])
            if not error_in_confirm:
                # /confirm HTTP 200, sin errores en body → probablemente éxito
                logger.info("Canje exitoso (/confirm HTTP 200, sin errores en body). Jugador: %s", player_name)
                return RedeemResponse(
                    success=True,
                    message="PIN canjeado exitosamente",
                    player_name=player_name,
                    details=f"confirm HTTP 200, body: {confirm_body[:200]}",
                )
            else:
                logger.warning("Error en body de /confirm: %s", confirm_body[:300])

        # Indicadores de que el formulario sigue ahí (NO se canjeó)
        still_on_form_keywords = [
            "editar dados", "editar datos", "edit data",
            "canjear ahora", "resgatar agora", "redeem now",
            "insira seu pin", "ingrese su pin",
        ]
        form_still_visible = any(kw in lower_text for kw in still_on_form_keywords)
        if form_still_visible:
            logger.warning("Página sigue en formulario - canje NO se completó")
            return RedeemResponse(
                success=False,
                message="Canje no completado: el formulario sigue visible",
                player_name=player_name,
                details=page_text[:400].strip(),
            )

        # Si no hay confirmación clara → FALLO
        snippet = page_text[:500].strip()
        logger.warning("Resultado incierto, reportando como FALLO: %s", snippet[:200])
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
        "browser_ready": _browser is not None,
    }


# ---------------------------------------------------------------------------
# Ejecutar con: python main.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, log_level="info")
