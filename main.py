import asyncio
import logging
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
# Globales – navegador pre-lanzado para mayor velocidad
# ---------------------------------------------------------------------------
_playwright = None
_browser: Browser | None = None

REDEEM_URL = "https://redeem.hype.games/client/es-MX/redeem"
TIMEOUT_MS = 30_000  # espera máxima por acción


# ---------------------------------------------------------------------------
# Ciclo de vida – iniciar / cerrar navegador con la app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser
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
    logger.info("Navegador pre-lanzado y listo.")
    yield
    # Apagado
    if _browser:
        await _browser.close()
    if _playwright:
        await _playwright.stop()
    logger.info("Navegador cerrado.")


app = FastAPI(title="Hype Games - Canjeador de PIN", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Modelos de petición / respuesta
# ---------------------------------------------------------------------------
class RedeemRequest(BaseModel):
    pin_key: str
    full_name: str
    birth_date: str   # formato esperado: DD/MM/YYYY (ej. "23/03/1998")
    player_id: str
    country: str


class RedeemResponse(BaseModel):
    success: bool
    message: str
    player_name: str | None = None
    details: str | None = None


# ---------------------------------------------------------------------------
# Auxiliar – crea un contexto nuevo (cookies/sesión aisladas) por petición
# ---------------------------------------------------------------------------
async def _new_context() -> BrowserContext:
    assert _browser is not None, "Navegador no inicializado"
    ctx = await _browser.new_context(
        viewport={"width": 1280, "height": 720},
        locale="es-ES",
    )
    return ctx


# ---------------------------------------------------------------------------
# Automatización principal
# ---------------------------------------------------------------------------
async def automate_redeem(data: RedeemRequest) -> RedeemResponse:
    ctx: BrowserContext | None = None
    try:
        ctx = await _new_context()
        page = await ctx.new_page()

        # ── 1. Navegar a la página de canje ─────────────────────────────
        logger.info("Navegando a %s", REDEEM_URL)
        await page.goto(REDEEM_URL, wait_until="networkidle", timeout=TIMEOUT_MS)

        # ── 2. Ingresar el PIN ────────────────────────────────────────
        # La página muestra "INSERTA TU PIN HYPE" con un campo de texto
        logger.info("Ingresando PIN...")
        pin_input = page.locator("input").first
        await pin_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        await pin_input.fill(data.pin_key)

        # Clic en botón "CANJEAR" / "REDEEM" / "RESGATAR"
        logger.info("Haciendo clic en botón de canje...")
        canjear_btn = page.locator(
            'button:has-text("CANJEAR"),'
            'button:has-text("REDEEM"),'
            'button:has-text("RESGATAR")'
        ).first
        await canjear_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
        await canjear_btn.click()

        # Esperar a que cargue el formulario (XHR: validate, countries, etc.)
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        await asyncio.sleep(1.5)

        # ── 3. Verificar errores de PIN ────────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        pin_error_keywords = [
            "already been redeemed", "already been used",
            "invalid pin", "pin inválido",
            "já foi utilizado", "pin not found",
            "código inválido", "invalid code",
            "pin ya fue", "ya fue canjeado", "ya fue utilizado",
            "no es válido", "not valid",
        ]
        for kw in pin_error_keywords:
            if kw.lower() in lower_text:
                logger.warning("Error de PIN detectado: %s", kw)
                return RedeemResponse(
                    success=False,
                    message="Error de PIN",
                    details=f"El sitio devolvió un error relacionado con: '{kw}'",
                )

        # Verificar que el formulario apareció
        form_keywords = ["nombre completo", "full name", "nome completo"]
        if not any(kw in lower_text for kw in form_keywords):
            snippet = page_text[:500].strip()
            return RedeemResponse(
                success=False,
                message="Página inesperada después de ingresar el PIN",
                details=snippet,
            )

        # ── 4. Llenar formulario de verificación ─────────────────────
        logger.info("Llenando formulario de verificación...")

        # Inputs visibles del formulario (en orden):
        # [0] Nombre Completo, [1] Fecha de Nacimiento, [2] ID de usuario
        form_inputs = page.locator("input[type='text'], input:not([type])")

        # Nombre Completo (primer input)
        name_input = form_inputs.nth(0)
        await name_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        await name_input.fill(data.full_name)

        # Fecha de Nacimiento (segundo input)
        date_input = form_inputs.nth(1)
        await date_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        await date_input.fill(data.birth_date)

        # Selecciona tu nacionalidad – es un dropdown <select>
        logger.info("Seleccionando país: %s", data.country)
        country_select = page.locator("select").first
        if await country_select.count() > 0:
            # Intentar seleccionar por texto visible
            try:
                await country_select.select_option(label=data.country)
            except Exception:
                # Respaldo: buscar coincidencia parcial por valor
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
                    return RedeemResponse(
                        success=False,
                        message="País no encontrado en el dropdown",
                        details=f"No se encontró '{data.country}' en el dropdown de nacionalidad",
                    )
        else:
            # Respaldo: dropdown personalizado (clic para abrir, luego seleccionar)
            dropdown_trigger = page.locator(
                'text="Selecciona tu nacionalidad",'
                '[class*="select" i],'
                '[class*="dropdown" i]'
            ).first
            await dropdown_trigger.click()
            await asyncio.sleep(0.5)
            await page.locator(f"text={data.country}").first.click()

        # ID de usuario en el juego
        id_input = form_inputs.nth(2)
        await id_input.wait_for(state="visible", timeout=TIMEOUT_MS)
        await id_input.fill(data.player_id)

        # ── 5. Marcar checkbox de términos y condiciones ──────────────
        logger.info("Marcando checkbox de términos y condiciones...")
        checkbox = page.locator('input[type="checkbox"]').first
        if await checkbox.count() > 0:
            is_checked = await checkbox.is_checked()
            if not is_checked:
                await checkbox.check()

        # ── 6. Clic en "VERIFICAR ID" / "VERIFY ID" ─────────────────────
        logger.info("Haciendo clic en VERIFICAR ID...")
        verify_btn = page.locator(
            'button:has-text("VERIFICAR ID"),'
            'button:has-text("VERIFY ID"),'
            'button:has-text("VERIFICAR")'
        ).first
        await verify_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
        await verify_btn.click()

        # Esperar a que se complete la petición XHR de account
        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        await asyncio.sleep(2)

        # ── 7. Verificar errores de ID ─────────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        id_error_keywords = [
            "id not found", "player not found",
            "id no encontrado", "id não encontrado",
            "invalid id", "id inválido",
            "does not exist", "no existe",
            "usuario no encontrado", "no se encontró",
        ]
        for kw in id_error_keywords:
            if kw.lower() in lower_text:
                logger.warning("Error de ID del jugador detectado: %s", kw)
                return RedeemResponse(
                    success=False,
                    message="Error de ID del jugador",
                    details=f"El sitio devolvió un error relacionado con: '{kw}'",
                )

        # ── 8. Extraer nombre del jugador ─────────────────────────────
        # Después de VERIFICAR ID, la página muestra "ID verificado ✓"
        # y el nombre del jugador en un recuadro oscuro (ej. "N7  LEOXZ7")
        player_name = None
        verified_marker = page.locator('text="ID verificado"')
        if await verified_marker.count() > 0:
            logger.info("ID verificado exitosamente, extrayendo nombre del jugador...")
            # El nombre del jugador aparece en un contenedor cerca de "ID verificado"
            # Se intentan múltiples estrategias para obtener el texto
            try:
                # Estrategia 1: Buscar el contenedor oscuro cerca del texto verificado
                # El nombre suele estar en un div/span estilizado después del badge
                name_container = page.locator(
                    '[class*="account" i],'
                    '[class*="player" i],'
                    '[class*="nickname" i],'
                    '[class*="user" i]'
                ).first
                if await name_container.count() > 0:
                    player_name = (await name_container.inner_text()).strip()
            except Exception:
                pass

            if not player_name:
                try:
                    # Estrategia 2: El texto entre "ID verificado" y
                    # "Verifica que los datos" contiene el nombre del jugador
                    full_text = page_text
                    start_marker = "ID verificado"
                    end_marker = "Verifica que los datos"
                    start_idx = full_text.find(start_marker)
                    end_idx = full_text.find(end_marker)
                    if start_idx != -1 and end_idx != -1:
                        between = full_text[start_idx + len(start_marker):end_idx].strip()
                        # Eliminar símbolos comunes como ✓
                        between = between.replace("✓", "").replace("✔", "").strip()
                        if between:
                            player_name = between
                except Exception:
                    pass

            if not player_name:
                try:
                    # Estrategia 3: Obtener bloques de texto
                    # El nombre está entre el badge de verificado y el botón de canjear
                    all_text = page_text
                    if "¡CANJEAR AHORA!" in all_text:
                        section = all_text.split("¡CANJEAR AHORA!")[0]
                        if "ID verificado" in section:
                            section = section.split("ID verificado")[-1]
                            section = section.replace("✓", "").replace("✔", "").strip()
                            lines = [l.strip() for l in section.split("\n") if l.strip()]
                            # Eliminar la línea "Verifica que los datos..."
                            lines = [l for l in lines if "verifica que" not in l.lower()]
                            if lines:
                                player_name = " ".join(lines)
                except Exception:
                    pass

            logger.info("Nombre del jugador extraído: %s", player_name)
        else:
            logger.warning("Marcador 'ID verificado' no encontrado en la página")

        # ── 9. Clic en "¡CANJEAR AHORA!" / "REDEEM NOW" ──────────────
        logger.info("Haciendo clic en CANJEAR AHORA...")
        redeem_btn = page.locator(
            'button:has-text("CANJEAR AHORA"),'
            'button:has-text("REDEEM NOW"),'
            'button:has-text("RESGATAR AGORA"),'
            'a:has-text("CANJEAR AHORA"),'
            'a:has-text("REDEEM NOW")'
        ).first
        await redeem_btn.wait_for(state="visible", timeout=TIMEOUT_MS)
        await redeem_btn.click()

        await page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
        await asyncio.sleep(2)

        # ── 10. Verificar resultado final ─────────────────────────────
        page_text = await page.inner_text("body")
        lower_text = page_text.lower()
        success_keywords = [
            "successfully redeemed", "canjeado con éxito",
            "resgatado com sucesso", "congratulations",
            "éxito", "canjeo exitoso", "fue canjeado",
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

        # Si no se encontró palabra clave de éxito pero se pasó el clic de canjear,
        # se trata como probablemente exitoso
        if player_name:
            return RedeemResponse(
                success=True,
                message="PIN canjeado (no se encontró texto de confirmación explícito)",
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
    finally:
        if ctx:
            await ctx.close()


# ---------------------------------------------------------------------------
# Endpoint de la API
# ---------------------------------------------------------------------------
@app.post("/redeem", response_model=RedeemResponse)
async def redeem_pin(data: RedeemRequest):
    logger.info("Petición de canje recibida para player_id=%s", data.player_id)
    result = await automate_redeem(data)
    return result


@app.get("/health")
async def health():
    return {"status": "ok", "browser_ready": _browser is not None}


# ---------------------------------------------------------------------------
# Ejecutar con: python main.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000, log_level="info")
