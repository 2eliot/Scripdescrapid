import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# ---------------------------------------------------------------------------
# Configuración de logs
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Globales
# ---------------------------------------------------------------------------
_playwright = None
_browser: Browser | None = None

# Página pre-calentada (warm page) para recargas rápidas
_warm_ctx: BrowserContext | None = None
_warm_page: Page | None = None
_warm_ready = False          # True cuando la página está lista en redeem.hype.games/
_warm_lock = asyncio.Lock()  # Solo 1 request a la vez usa la warm page

REDEEM_URL = "https://redeem.hype.games/"
TIMEOUT_MS = 30_000


# ---------------------------------------------------------------------------
# Warm page management
# ---------------------------------------------------------------------------
async def _create_warm_page():
    """Crea y navega una página pre-calentada lista para recibir PINes."""
    global _warm_ctx, _warm_page, _warm_ready
    assert _browser is not None
    _warm_ctx = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="pt-BR",
    )
    _warm_page = await _warm_ctx.new_page()
    logger.info("[WARM] Navegando a %s ...", REDEEM_URL)
    await _warm_page.goto(REDEEM_URL, wait_until="networkidle", timeout=TIMEOUT_MS)
    await asyncio.sleep(2)  # Cloudflare Rocket Loader
    _warm_ready = True
    logger.info("[WARM] Página pre-calentada y lista ✓")


async def _reset_warm_page():
    """Después de un canje, navega de vuelta a / para estar lista."""
    global _warm_ready
    _warm_ready = False
    try:
        if _warm_page and not _warm_page.is_closed():
            await _warm_page.goto(REDEEM_URL, wait_until="networkidle", timeout=TIMEOUT_MS)
            await asyncio.sleep(1)
            _warm_ready = True
            logger.info("[WARM] Página re-calentada ✓")
        else:
            await _create_warm_page()
    except Exception as e:
        logger.warning("[WARM] Error al recalentar, creando nueva: %s", e)
        try:
            if _warm_ctx:
                await _warm_ctx.close()
        except Exception:
            pass
        await _create_warm_page()


async def _get_page_for_request():
    """Obtiene una página lista. Usa la warm si está disponible, si no crea una nueva."""
    global _warm_ready
    if _warm_ready and _warm_page and not _warm_page.is_closed():
        _warm_ready = False  # Marcar como en uso
        logger.info("[WARM] Usando página pre-calentada (rápido)")
        return _warm_page, False  # (page, is_cold)

    # Página fría — crear contexto nuevo
    logger.info("[COLD] Creando página nueva (primera vez o warm ocupada)")
    ctx = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="pt-BR",
    )
    page = await ctx.new_page()
    await page.goto(REDEEM_URL, wait_until="networkidle", timeout=TIMEOUT_MS)
    await asyncio.sleep(2)
    return page, True  # (page, is_cold)


# ---------------------------------------------------------------------------
# Ciclo de vida
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser, _warm_lock
    _warm_lock = asyncio.Lock()
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    logger.info("Navegador pre-lanzado.")
    # Pre-calentar una página
    await _create_warm_page()
    yield
    try:
        if _warm_ctx:
            await _warm_ctx.close()
    except Exception:
        pass
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
# Automatización principal (con warm page)
# ---------------------------------------------------------------------------
async def automate_redeem(data: RedeemRequest) -> RedeemResponse:
    is_cold = False
    start = time.time()
    try:
        page, is_cold = await _get_page_for_request()
        elapsed = time.time() - start
        logger.info("Página lista en %.1fs (%s)", elapsed, "cold" if is_cold else "warm")

        # ── 2. Ingresar el PIN ────────────────────────────────────────
        logger.info("Ingresando PIN...")
        pin_input = page.locator("#pininput")
        await pin_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        await pin_input.fill(data.pin_key)

        # Esperar a que el botón se habilite (el JS lo habilita cuando el PIN es válido)
        logger.info("Esperando que botón Verificar se habilite...")
        btn_validate = page.locator("#btn-validate")
        await btn_validate.wait_for(state="visible", timeout=TIMEOUT_MS)

        # Si autoSubmitPin=true el form se envía solo; si no, esperamos que se habilite
        # Intentar esperar a que el botón no tenga "disabled"
        for _ in range(20):
            disabled = await btn_validate.get_attribute("disabled")
            if disabled is None:
                break
            await asyncio.sleep(0.5)

        logger.info("Haciendo clic en Verificar...")
        await btn_validate.click()

        # Esperar a que la tarjeta haga flip (aparece .card.back con el formulario)
        logger.info("Esperando flip de tarjeta...")
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
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

        # ── 4. Llenar formulario de verificación ─────────────────────
        logger.info("Llenando formulario...")

        # Nombre Completo (input#Name)
        name_input = page.locator("#Name")
        if await name_input.count() > 0:
            await name_input.fill(data.full_name)
            logger.info("Nombre: %s", data.full_name)
        else:
            # Fallback: primer input de texto visible en .card.back
            name_input = card_back.locator("input[type='text']").first
            await name_input.fill(data.full_name)

        # Fecha de Nacimiento (input#BornAt)
        date_input = page.locator("#BornAt")
        if await date_input.count() > 0:
            await date_input.fill(data.birth_date)
            logger.info("Fecha: %s", data.birth_date)
        else:
            date_input = card_back.locator("input[type='text']").nth(1)
            await date_input.fill(data.birth_date)

        # Nacionalidad (select#NationalityAlphaCode)
        logger.info("Seleccionando país: %s", data.country)
        country_select = page.locator("#NationalityAlphaCode")
        if await country_select.count() > 0:
            await asyncio.sleep(1)  # Esperar a que se carguen las opciones via AJAX
            try:
                await country_select.select_option(label=data.country)
            except Exception:
                # Buscar coincidencia parcial
                options = await country_select.locator("option").all()
                matched = False
                for opt in options:
                    opt_text = (await opt.inner_text()).strip()
                    if data.country.lower() in opt_text.lower():
                        value = await opt.get_attribute("value")
                        await country_select.select_option(value=value)
                        matched = True
                        break
                if not matched:
                    logger.warning("País '%s' no encontrado en dropdown", data.country)

        # ID de usuario en el juego (input#GameAccountId)
        id_input = page.locator("#GameAccountId")
        if await id_input.count() > 0:
            await id_input.fill(data.player_id)
            logger.info("Player ID: %s", data.player_id)
        else:
            # Fallback
            id_input = card_back.locator("input[type='text']").nth(2)
            await id_input.fill(data.player_id)

        # ── 5. Marcar checkbox de privacidad ──────────────────────────
        logger.info("Marcando checkbox de privacidad...")
        checkbox = page.locator("#privacy")
        if await checkbox.count() > 0:
            is_checked = await checkbox.is_checked()
            if not is_checked:
                await checkbox.check()
        else:
            checkbox = page.locator('input[type="checkbox"]').first
            if await checkbox.count() > 0:
                if not await checkbox.is_checked():
                    await checkbox.check()

        # ── 6. Clic en botón VERIFICAR ID ────────────────────────────
        # El JS llama a validate/account via AJAX que retorna {"Success":true,"Username":"NOMBRE"}
        # Interceptamos esa respuesta para obtener el player_name directamente
        player_name = None

        logger.info("Buscando botón de verificar ID...")
        verify_btn = page.locator(
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
                await verify_btn.click()

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

            await asyncio.sleep(2)
        else:
            logger.warning("Botón Verificar ID no encontrado, continuando...")

        # ── 8. Clic en botón final de canje ──────────────────────────
        # El form tiene action="/confirm". El botón submit puede tener
        # textos: "Resgatar Agora", "Canjear Ahora", "Redeem Now", "Confirmar"
        logger.info("Buscando botón de canje final...")
        redeem_btn = page.locator(
            'button:has-text("Resgatar"),'
            'button:has-text("Canjear"),'
            'button:has-text("Redeem"),'
            'button:has-text("Confirmar"),'
            'form[action="/confirm"] button[type="submit"],'
            '#btn-confirm'
        ).first

        if await redeem_btn.count() > 0:
            logger.info("Haciendo clic en botón de canje final...")
            await redeem_btn.click()
        else:
            # Fallback: submit del form de confirmación
            logger.info("Buscando form[action=/confirm] para submit...")
            confirm_form = page.locator('form[action="/confirm"]')
            if await confirm_form.count() > 0:
                submit_btn = confirm_form.locator('button, input[type="submit"]').first
                if await submit_btn.count() > 0:
                    await submit_btn.click()
                else:
                    logger.warning("No se encontró botón de submit en form /confirm")
                    return RedeemResponse(
                        success=False,
                        message="No se encontró botón de canje final",
                        details=page_text[:300],
                    )
            else:
                return RedeemResponse(
                    success=False,
                    message="No se encontró formulario de confirmación",
                    details=page_text[:300],
                )

        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        await asyncio.sleep(3)

        # ── 9. Verificar resultado final ─────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        logger.info("Resultado final (200 chars): %s", page_text[:200].replace("\n", " "))

        success_keywords = [
            "successfully redeemed", "canjeado con éxito",
            "resgatado com sucesso", "congratulations",
            "éxito", "canjeo exitoso", "fue canjeado",
            "resgatado", "sucesso", "parabéns",
        ]
        for kw in success_keywords:
            if kw.lower() in lower_text:
                logger.info("¡Canje exitoso! Jugador: %s", player_name)
                return RedeemResponse(
                    success=True,
                    message="PIN canjeado exitosamente",
                    player_name=player_name,
                    details=kw,
                )

        # Si tenemos player_name y no hay errores claros, asumir éxito
        if player_name:
            return RedeemResponse(
                success=True,
                message="PIN canjeado (sin confirmación explícita)",
                player_name=player_name,
                details=page_text[:300].strip(),
            )

        snippet = page_text[:500].strip()
        return RedeemResponse(
            success=False,
            message="Resultado incierto – revisa los detalles",
            details=snippet,
        )

    except Exception as exc:
        logger.exception("Error de automatización")
        return RedeemResponse(
            success=False,
            message="Error de automatización",
            details=str(exc),
        )


# ---------------------------------------------------------------------------
# Endpoint de la API (con lock para serializar requests + recalentar página)
# ---------------------------------------------------------------------------
@app.post("/redeem", response_model=RedeemResponse)
async def redeem_pin(data: RedeemRequest):
    logger.info("Petición de canje recibida para player_id=%s", data.player_id)
    async with _warm_lock:  # Solo 1 canje a la vez
        result = await automate_redeem(data)
        # Recalentar página para el siguiente request (en background)
        asyncio.create_task(_reset_warm_page())
    return result


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "browser_ready": _browser is not None,
        "warm_page_ready": _warm_ready,
    }


# ---------------------------------------------------------------------------
# Ejecutar con: python main.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, log_level="info")
