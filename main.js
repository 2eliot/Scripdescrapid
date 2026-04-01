const fastify = require('fastify')({ logger: true });
const { chromium } = require('playwright');

const REDEEM_URL = 'https://redeem.hype.games/';
const TIMEOUT_MS = 30_000;
const MAX_CONCURRENT = Number.parseInt(process.env.MAX_CONCURRENT_REDEEMS || '3', 10);
const IDEMPOTENCY_CACHE_MAX = 500;

const PIN_ERROR_KEYWORDS = [
    'already been redeemed', 'already been used',
    'invalid pin', 'pin inválido', 'pin inv',
    'já foi utilizado', 'pin not found',
    'código inválido', 'invalid code',
    'pin ya fue', 'ya fue canjeado',
    'not valid', 'não é válido',
    'já foi resgatado', 'expirado', 'expired',
];

const SUCCESS_KEYWORDS = [
    'successfully redeemed', 'canjeado con éxito',
    'resgatado com sucesso', 'congratulations',
    'canjeo exitoso', 'fue canjeado',
    'parabéns', 'felicidades',
    'your order has been', 'pedido foi',
];

const FORM_KEYWORDS = [
    'nome completo', 'nombre completo', 'full name',
    'gameaccountid', 'id do jogador', 'id de usuario',
];

const STILL_ON_FORM_KEYWORDS = [
    'editar dados', 'editar datos', 'edit data',
    'canjear ahora', 'resgatar agora', 'redeem now',
    'insira seu pin', 'ingrese su pin',
];

const CONFIRM_ERROR_KEYWORDS = [
    'error', 'erro', 'failed', 'invalid', 'expired',
    'falhou', 'falló', 'tente novamente', 'try again',
];

const BLOCKED_DOMAINS = [
    'google-analytics.com', 'googletagmanager.com',
    'facebook.net', 'facebook.com', 'fbcdn.net',
    'hotjar.com', 'doubleclick.net', 'googlesyndication.com',
    'cloudflareinsights.com', 'clarity.ms', 'connect.facebook.net',
];

const BROWSER_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-software-rasterizer',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-default-apps',
    '--disable-sync',
    '--disable-translate',
    '--disable-component-update',
    '--no-first-run',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-ipc-flooding-protection',
    '--js-flags=--max-old-space-size=256',
];

let browser;
let browserLaunchPromise = null;
let activeContexts = 0;
let totalRedeems = 0;
const idempotencyCache = new Map();
let processCpuUsage = process.cpuUsage();
let processHrtime = process.hrtime.bigint();

class Semaphore {
    constructor(max) {
        this.max = max;
        this.current = 0;
        this.queue = [];
    }

    async acquire() {
        if (this.current < this.max) {
            this.current += 1;
            return this.createRelease();
        }

        await new Promise((resolve) => {
            this.queue.push(resolve);
        });

        this.current += 1;
        return this.createRelease();
    }

    createRelease() {
        let released = false;

        return () => {
            if (released) {
                return;
            }

            released = true;
            this.current -= 1;

            const next = this.queue.shift();
            if (next) {
                next();
            }
        };
    }
}

const redeemSemaphore = new Semaphore(MAX_CONCURRENT);

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

function getBrowserReady() {
    try {
        return Boolean(browser && browser.isConnected());
    } catch {
        return false;
    }
}

async function closeBrowser() {
    if (!browser) {
        return;
    }

    try {
        await browser.close();
    } catch (error) {
        fastify.log.warn({ err: error }, 'Error cerrando browser');
    } finally {
        browser = undefined;
    }
}

async function launchBrowser() {
    browser = await chromium.launch({
        headless: true,
        args: BROWSER_ARGS,
    });

    fastify.log.info({ maxConcurrent: MAX_CONCURRENT }, 'Navegador Chromium listo');
    return browser;
}

async function ensureBrowser() {
    if (getBrowserReady()) {
        return browser;
    }

    if (browserLaunchPromise) {
        return browserLaunchPromise;
    }

    browserLaunchPromise = (async () => {
        fastify.log.warn('Browser caído o no inicializado, relanzando...');
        await closeBrowser();
        return launchBrowser();
    })();

    try {
        return await browserLaunchPromise;
    } finally {
        browserLaunchPromise = null;
    }
}

function getCachedResult(requestId) {
    if (!requestId || !idempotencyCache.has(requestId)) {
        return null;
    }

    return structuredClone(idempotencyCache.get(requestId));
}

function setCachedResult(requestId, result) {
    if (!requestId) {
        return;
    }

    if (idempotencyCache.size >= IDEMPOTENCY_CACHE_MAX) {
        const oldestKey = idempotencyCache.keys().next().value;
        if (oldestKey) {
            idempotencyCache.delete(oldestKey);
        }
    }

    idempotencyCache.set(requestId, structuredClone(result));
}

function getCpuPercent() {
    const currentCpu = process.cpuUsage();
    const currentHr = process.hrtime.bigint();
    const cpuDiff = (currentCpu.user - processCpuUsage.user) + (currentCpu.system - processCpuUsage.system);
    const timeDiffNs = Number(currentHr - processHrtime);

    processCpuUsage = currentCpu;
    processHrtime = currentHr;

    if (timeDiffNs <= 0) {
        return 0;
    }

    return Number(((cpuDiff / 1000) / (timeDiffNs / 1_000_000)) * 100).toFixed(1);
}

async function automateRedeem(data) {
    const startedAt = Date.now();
    let context;

    try {
        const currentBrowser = await ensureBrowser();
        context = await currentBrowser.newContext({
            viewport: { width: 1024, height: 600 },
            locale: 'pt-BR',
        });

        await context.route('**/*', async (route) => {
            const request = route.request();
            const resourceType = request.resourceType();
            const url = request.url();

            if (['image', 'font', 'media'].includes(resourceType) || BLOCKED_DOMAINS.some((domain) => url.includes(domain))) {
                await route.abort();
                return;
            }

            await route.continue();
        });

        const page = await context.newPage();

        fastify.log.info({ url: REDEEM_URL }, 'Navegando a Hype Games');
        await page.goto(REDEEM_URL, { waitUntil: 'domcontentloaded', timeout: TIMEOUT_MS });
        await page.waitForSelector('#pininput', { state: 'visible', timeout: TIMEOUT_MS });
        fastify.log.info({ elapsedMs: Date.now() - startedAt }, 'Página lista');

        try {
            const cookieDismissed = await page.evaluate(() => {
                const selectors = [
                    'button[id*="accept"]', 'button[id*="Accept"]',
                    'a[id*="accept"]', 'a[id*="Accept"]',
                    '.cc-accept', '.cc-dismiss',
                    'button.accept-cookies',
                ];
                for (const selector of selectors) {
                    const element = document.querySelector(selector);
                    if (element) {
                        element.click();
                        return `clicked:${selector}`;
                    }
                }

                const buttons = document.querySelectorAll('button, a.btn, a[role="button"]');
                for (const button of buttons) {
                    const text = button.textContent.trim().toLowerCase();
                    if (
                        text === 'aceptar' ||
                        text === 'accept' ||
                        text === 'aceitar' ||
                        text === 'accept all' ||
                        text === 'aceptar todo' ||
                        text === 'aceptar todas'
                    ) {
                        button.click();
                        return `clicked_text:${text}`;
                    }
                }

                document
                    .querySelectorAll('[class*="cookie"], [class*="consent"], [id*="cookie"], [id*="consent"]')
                    .forEach((element) => element.remove());

                return 'no_btn_found_overlays_removed';
            });
            fastify.log.info({ cookieDismissed }, 'Cookie popup procesado');
        } catch (error) {
            fastify.log.warn({ err: error }, 'Cookie popup dismiss falló');
        }

        let recaptchaReady = false;
        for (let attempt = 0; attempt < 20; attempt += 1) {
            recaptchaReady = await page.evaluate(
                () => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function',
            );
            if (recaptchaReady) {
                break;
            }
            await sleep(150);
        }
        fastify.log.info({ recaptchaReady }, 'Estado de reCAPTCHA');

        const pinInput = page.locator('#pininput');
        await pinInput.waitFor({ state: 'visible', timeout: TIMEOUT_MS });
        await pinInput.fill(data.pin_key);

        const btnValidate = page.locator('#btn-validate');
        await btnValidate.waitFor({ state: 'visible', timeout: TIMEOUT_MS });

        for (let attempt = 0; attempt < 30; attempt += 1) {
            const disabled = await btnValidate.getAttribute('disabled');
            if (disabled === null) {
                break;
            }
            await sleep(150);
        }

        try {
            const validateResponsePromise = page.waitForResponse(
                (response) => response.url().includes('/validate') && !response.url().includes('account'),
                { timeout: TIMEOUT_MS },
            );
            await btnValidate.click();
            const validateResponse = await validateResponsePromise;
            fastify.log.info({ status: validateResponse.status() }, 'Respuesta /validate');

            if (validateResponse.status() >= 400) {
                const body = await validateResponse.text();
                fastify.log.warn({ body: body.slice(0, 300) }, 'Error HTTP en /validate');
            }
        } catch (error) {
            fastify.log.warn({ err: error }, 'No se pudo interceptar /validate');
        }

        try {
            await page.locator('.card.back').waitFor({ state: 'visible', timeout: 15_000 });
        } catch {
            await sleep(500);
        }

        let pageText = await page.innerText('body');
        let lowerText = pageText.toLowerCase();

        const pinKeyword = PIN_ERROR_KEYWORDS.find((keyword) => lowerText.includes(keyword.toLowerCase()));
        if (pinKeyword) {
            return {
                success: false,
                message: 'Error de PIN',
                details: `El sitio devolvió un error: '${pinKeyword}'`,
            };
        }

        const cardBack = page.locator('.card.back');
        let cardBackHtml = '';
        if (await cardBack.count() > 0) {
            cardBackHtml = await cardBack.first().innerHTML();
        }

        if (!cardBackHtml || !cardBackHtml.includes('GameAccountId')) {
            if (!FORM_KEYWORDS.some((keyword) => lowerText.includes(keyword))) {
                return {
                    success: false,
                    message: 'Formulario no apareció después de validar PIN',
                    details: pageText.slice(0, 500).trim(),
                };
            }
        }

        await page.evaluate((payload) => {
            const nameElement = document.querySelector('#Name');
            const bornElement = document.querySelector('#BornAt');
            const idElement = document.querySelector('#GameAccountId');

            if (nameElement) {
                nameElement.value = payload.full_name;
                nameElement.dispatchEvent(new Event('input', { bubbles: true }));
            }

            if (bornElement) {
                bornElement.value = payload.birth_date;
                bornElement.dispatchEvent(new Event('input', { bubbles: true }));
            }

            if (idElement) {
                idElement.value = payload.player_id;
                idElement.dispatchEvent(new Event('input', { bubbles: true }));
            }
        }, data);

        const countrySelect = page.locator('#NationalityAlphaCode');
        let optionCount = 0;
        for (let attempt = 0; attempt < 20; attempt += 1) {
            optionCount = await countrySelect.evaluate((element) => element.options.length);
            if (optionCount > 1) {
                break;
            }
            await sleep(100);
        }

        fastify.log.info({ optionCount }, 'Opciones de país cargadas');

        let countrySelected = false;
        const countryName = data.country.toLowerCase();

        try {
            const targetValue = await countrySelect.evaluate((element, loweredCountryName) => {
                for (const option of element.options) {
                    if (option.text.toLowerCase().includes(loweredCountryName)) {
                        return option.value;
                    }
                }
                return null;
            }, countryName);

            if (targetValue) {
                await countrySelect.selectOption({ value: targetValue });
                countrySelected = true;
                fastify.log.info({ targetValue }, 'País seleccionado');
            }
        } catch (error) {
            fastify.log.warn({ err: error }, 'Error seleccionando país');
        }

        if (!countrySelected) {
            try {
                const fallbackValue = await countrySelect.evaluate((element) => {
                    for (const option of element.options) {
                        if (option.text.toLowerCase().includes('chile')) {
                            return option.value;
                        }
                    }

                    for (const option of element.options) {
                        if (option.value) {
                            return option.value;
                        }
                    }

                    return null;
                });

                if (fallbackValue) {
                    await countrySelect.selectOption({ value: fallbackValue });
                    fastify.log.info({ fallbackValue }, 'Fallback de país aplicado');
                }
            } catch (error) {
                fastify.log.warn({ err: error }, 'Fallback de país falló');
            }
        }

        let playerName = null;

        await page.evaluate(() => {
            const buttons = document.querySelectorAll('#btn-verify, #btn-verify-account, .btn-verify');
            buttons.forEach((button) => button.removeAttribute('disabled'));
        });

        const verifyBtn = page.locator(
            '#btn-verify, button:has-text("Verificar ID"), button:has-text("Verify ID"), button:has-text("Verificar Id"), #btn-verify-account',
        ).first();

        if (await verifyBtn.count() > 0) {
            try {
                const accountResponsePromise = page.waitForResponse(
                    (response) => response.url().includes('validate/account'),
                    { timeout: TIMEOUT_MS },
                );
                await verifyBtn.click({ timeout: 5000 });
                const accountResponse = await accountResponsePromise;
                const accountJson = await accountResponse.json();

                if (accountJson.Success) {
                    playerName = accountJson.Username || null;
                } else {
                    return {
                        success: false,
                        message: 'Error de ID del jugador',
                        details: accountJson.Message || 'ID inválido',
                    };
                }
            } catch (error) {
                fastify.log.warn({ err: error }, 'No se pudo procesar validate/account');
            }

            await sleep(100);
        } else {
            fastify.log.warn('Botón Verificar ID no encontrado, continuando');
        }

        await page.evaluate(() => {
            document.querySelectorAll('input[type="checkbox"]').forEach((checkbox) => {
                checkbox.checked = false;
            });
        });

        const checkboxes = page.locator('input[type="checkbox"]');
        const checkboxCount = await checkboxes.count();
        for (let index = 0; index < checkboxCount; index += 1) {
            const checkbox = checkboxes.nth(index);
            let checkboxId = `idx${index}`;

            try {
                checkboxId = (await checkbox.getAttribute('id')) || checkboxId;
                if (await checkbox.isVisible()) {
                    await checkbox.click({ timeout: 3000 });
                    continue;
                }

                const label = page.locator(`label[for="${checkboxId}"]`).first();
                if (await label.count() > 0 && await label.isVisible()) {
                    await label.click({ timeout: 3000 });
                    continue;
                }

                await checkbox.evaluate((element) => element.click());
            } catch (error) {
                fastify.log.warn({ err: error, checkboxId }, 'No se pudo marcar checkbox');
            }
        }

        await sleep(100);

        await page.evaluate(() => {
            document.querySelectorAll(
                '[class*="cookie"], [class*="consent"], [id*="cookie"], [id*="consent"], ' +
                '[class*="Cookie"], [class*="Consent"], .cc-window, .cc-banner, #onetrust-banner-sdk',
            ).forEach((element) => element.remove());

            document.querySelectorAll('[class*="overlay"], [class*="backdrop"], [class*="modal"]').forEach((element) => {
                if (element.id !== 'btn-redeem' && !element.closest('.card')) {
                    element.remove();
                }
            });

            const button = document.querySelector('#btn-redeem');
            if (button) {
                button.removeAttribute('disabled');
            }
        });

        const urlBefore = page.url();
        let confirmOk = false;
        let confirmBody = '';

        const redeemBtn = page.locator('#btn-redeem').first();
        if (await redeemBtn.count() > 0 && await redeemBtn.isVisible()) {
            try {
                const confirmResponsePromise = page.waitForResponse(
                    (response) => response.url().includes('/confirm'),
                    { timeout: 10_000 },
                );
                await redeemBtn.click({ timeout: 5000 });
                const confirmResponse = await confirmResponsePromise;
                confirmBody = await confirmResponse.text().catch(() => '');
                if (confirmResponse.status() < 400) {
                    confirmOk = true;
                }
            } catch (error) {
                fastify.log.warn({ err: error }, 'Intento 1 de submit falló');
            }
        }

        if (!confirmOk && !confirmBody) {
            const recaptchaDiag = await page.evaluate(() => {
                const diag = {
                    hasGrecaptcha: Boolean(window.grecaptcha),
                    hasExecute: Boolean(window.grecaptcha && window.grecaptcha.execute),
                    sitekey: null,
                    method: null,
                };

                const sitekeyElement = document.querySelector('[data-sitekey]');
                if (sitekeyElement) {
                    diag.sitekey = sitekeyElement.getAttribute('data-sitekey');
                    diag.method = 'data-sitekey';
                    return diag;
                }

                const iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                for (const iframe of iframes) {
                    const match = iframe.src.match(/[?&]k=([^&]+)/);
                    if (match) {
                        diag.sitekey = match[1];
                        diag.method = 'iframe_src';
                        return diag;
                    }
                }

                const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                for (const script of scripts) {
                    const match = script.src.match(/render=([^&]+)/);
                    if (match) {
                        diag.sitekey = match[1];
                        diag.method = 'script_render';
                        return diag;
                    }
                }

                try {
                    const config = window.___grecaptcha_cfg;
                    if (config && config.clients) {
                        for (const clientId of Object.keys(config.clients)) {
                            const client = config.clients[clientId];
                            if (client && client.Hm) {
                                diag.sitekey = client.Hm;
                                diag.method = 'grecaptcha_cfg_Hm';
                                return diag;
                            }

                            const json = JSON.stringify(client);
                            const match = json.match(/6L[a-zA-Z0-9_-]{38,}/);
                            if (match) {
                                diag.sitekey = match[0];
                                diag.method = 'grecaptcha_cfg_regex';
                                return diag;
                            }
                        }
                    }
                } catch {}

                const html = document.documentElement.innerHTML;
                const htmlMatch = html.match(/6L[a-zA-Z0-9_-]{38,}/);
                if (htmlMatch) {
                    diag.sitekey = htmlMatch[0];
                    diag.method = 'html_regex';
                }

                return diag;
            });

            if (recaptchaDiag.sitekey && recaptchaDiag.hasExecute) {
                try {
                    const confirmResponsePromise = page.waitForResponse(
                        (response) => response.url().includes('/confirm'),
                        { timeout: 15_000 },
                    );
                    await page.evaluate((sitekey) => new Promise((resolve) => {
                        window.grecaptcha.execute(sitekey, { action: 'confirm' }).then((token) => {
                            let input = document.querySelector('#g-recaptcha-response') ||
                                document.querySelector('textarea[name="g-recaptcha-response"]');

                            if (!input) {
                                document.querySelectorAll('textarea').forEach((textarea) => {
                                    if (!input && textarea.name && textarea.name.includes('recaptcha')) {
                                        input = textarea;
                                    }
                                });
                            }

                            if (input) {
                                input.value = token;
                                input.innerHTML = token;
                            }

                            const button = document.querySelector('#btn-redeem');
                            if (button) {
                                button.removeAttribute('disabled');
                                button.click();
                                resolve(true);
                                return;
                            }

                            resolve(false);
                        }).catch(() => resolve(false));
                    }), recaptchaDiag.sitekey);
                    const confirmResponse = await confirmResponsePromise;
                    confirmBody = await confirmResponse.text().catch(() => '');
                    if (confirmResponse.status() < 400) {
                        confirmOk = true;
                    }
                } catch (error) {
                    fastify.log.warn({ err: error }, 'Intento 2 de submit falló');
                }
            }
        }

        if (!confirmOk && !confirmBody) {
            try {
                const confirmResponsePromise = page.waitForResponse(
                    (response) => response.url().includes('/confirm'),
                    { timeout: 15_000 },
                );
                await page.evaluate(() => {
                    const form = document.querySelector('form');
                    if (form) {
                        form.submit();
                    }
                });
                const confirmResponse = await confirmResponsePromise;
                confirmBody = await confirmResponse.text().catch(() => '');
                if (confirmResponse.status() < 400) {
                    confirmOk = true;
                }
            } catch (error) {
                fastify.log.warn({ err: error }, 'Intento 3 de submit falló');
            }
        }

        if (!confirmOk && !confirmBody) {
            return {
                success: false,
                message: 'No se pudo enviar el formulario de canje',
                player_name: playerName,
                details: 'Ambos intentos principales de submit fallaron',
            };
        }

        await sleep(100);

        const urlAfter = page.url();
        fastify.log.info({ urlBefore, urlAfter, changed: urlAfter !== urlBefore }, 'Estado de URL tras submit');

        pageText = await page.innerText('body');
        lowerText = pageText.toLowerCase();
        const combinedText = `${lowerText} ${confirmBody.toLowerCase()}`.trim();

        const successKeyword = SUCCESS_KEYWORDS.find((keyword) => combinedText.includes(keyword));
        if (successKeyword) {
            return {
                success: true,
                message: 'PIN canjeado exitosamente',
                player_name: playerName,
                details: successKeyword,
            };
        }

        if (confirmOk && confirmBody) {
            try {
                const confirmJson = JSON.parse(confirmBody);
                if (confirmJson && typeof confirmJson === 'object') {
                    if (confirmJson.Success === true) {
                        return {
                            success: true,
                            message: 'PIN canjeado exitosamente',
                            player_name: playerName,
                            details: `confirm JSON: ${confirmBody.slice(0, 200)}`,
                        };
                    }

                    return {
                        success: false,
                        message: `Error del servidor: ${confirmJson.Message || 'Respuesta inválida'}`,
                        player_name: playerName,
                        details: confirmBody.slice(0, 300),
                    };
                }
            } catch {}

            const confirmLower = confirmBody.toLowerCase();
            const hasConfirmError = CONFIRM_ERROR_KEYWORDS.some((keyword) => confirmLower.includes(keyword));
            if (!hasConfirmError) {
                return {
                    success: true,
                    message: 'PIN canjeado exitosamente',
                    player_name: playerName,
                    details: `confirm HTTP 200, body: ${confirmBody.slice(0, 200)}`,
                };
            }
        }

        if (STILL_ON_FORM_KEYWORDS.some((keyword) => lowerText.includes(keyword))) {
            return {
                success: false,
                message: 'Canje no completado: el formulario sigue visible',
                player_name: playerName,
                details: pageText.slice(0, 400).trim(),
            };
        }

        return {
            success: false,
            message: 'Resultado incierto – no se confirmó el canje',
            player_name: playerName,
            details: pageText.slice(0, 500).trim(),
        };
    } catch (error) {
        fastify.log.error({ err: error }, 'Error de automatización');
        return {
            success: false,
            message: 'Error de automatización',
            details: error.message,
        };
    } finally {
        if (context) {
            try {
                await context.close();
            } catch {}
        }

        fastify.log.info({ elapsedMs: Date.now() - startedAt }, 'Canje completado');
    }
}

const redeemBodySchema = {
    type: 'object',
    required: ['pin_key', 'full_name', 'birth_date', 'player_id', 'country'],
    additionalProperties: false,
    properties: {
        pin_key: { type: 'string', minLength: 1 },
        full_name: { type: 'string', minLength: 1 },
        birth_date: { type: 'string', minLength: 1 },
        player_id: { type: 'string', minLength: 1 },
        country: { type: 'string', minLength: 1 },
        request_id: { type: 'string' },
    },
};

fastify.post('/redeem', { schema: { body: redeemBodySchema } }, async (request) => {
    const data = request.body;
    fastify.log.info(
        { playerId: data.player_id, activeContexts, maxConcurrent: MAX_CONCURRENT },
        'Petición de canje recibida',
    );

    const cached = getCachedResult(data.request_id);
    if (cached) {
        fastify.log.info({ requestId: data.request_id, success: cached.success }, 'Resultado devuelto desde cache');
        return cached;
    }

    const release = await redeemSemaphore.acquire();
    activeContexts += 1;
    totalRedeems += 1;

    let result;
    try {
        result = await automateRedeem(data);
    } finally {
        activeContexts -= 1;
        release();
    }

    setCachedResult(data.request_id, result);
    return result;
});

fastify.get('/health', async () => ({
    status: 'ok',
    browser_ready: getBrowserReady(),
}));

fastify.get('/metrics', async () => {
    const mem = process.memoryUsage();

    return {
        rss_mb: Number((mem.rss / 1024 / 1024).toFixed(1)),
        heap_used_mb: Number((mem.heapUsed / 1024 / 1024).toFixed(1)),
        heap_total_mb: Number((mem.heapTotal / 1024 / 1024).toFixed(1)),
        external_mb: Number((mem.external / 1024 / 1024).toFixed(1)),
        cpu_percent: Number(getCpuPercent()),
        browser_connected: getBrowserReady(),
        active_contexts: activeContexts,
        max_concurrent: MAX_CONCURRENT,
        total_redeems: totalRedeems,
        idempotency_cache_size: idempotencyCache.size,
    };
});

fastify.addHook('onClose', async () => {
    await closeBrowser();
});

async function start() {
    try {
        await ensureBrowser();
        const port = Number.parseInt(process.env.PORT || '5000', 10);
        const host = process.env.HOST || '0.0.0.0';
        await fastify.listen({ port, host });
    } catch (error) {
        fastify.log.error({ err: error }, 'No se pudo iniciar el servidor');
        process.exit(1);
    }
}

async function shutdown(signal) {
    fastify.log.info({ signal }, 'Cerrando servicio');
    try {
        await fastify.close();
        process.exit(0);
    } catch (error) {
        fastify.log.error({ err: error }, 'Error cerrando servicio');
        process.exit(1);
    }
}

process.on('SIGINT', () => {
    void shutdown('SIGINT');
});

process.on('SIGTERM', () => {
    void shutdown('SIGTERM');
});

void start();