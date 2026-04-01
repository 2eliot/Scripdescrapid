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

// --- Page Pool ---
const pagePool = [];        // Array of { context, page, ready: boolean }
let poolFilling = false;

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

// --- Page Pool: create a pre-warmed page ---
async function createWarmedPage() {
    const currentBrowser = await ensureBrowser();
    const context = await currentBrowser.newContext({
        viewport: { width: 1024, height: 600 },
        locale: 'pt-BR',
    });

    await context.route('**/*', async (route) => {
        const request = route.request();
        const resourceType = request.resourceType();
        const url = request.url();

        if (['image', 'font', 'media', 'stylesheet'].includes(resourceType) || BLOCKED_DOMAINS.some((domain) => url.includes(domain))) {
            await route.abort();
            return;
        }

        await route.continue();
    });

    const page = await context.newPage();
    await page.goto(REDEEM_URL, { waitUntil: 'domcontentloaded', timeout: TIMEOUT_MS });
    await page.waitForSelector('#pininput', { state: 'visible', timeout: TIMEOUT_MS });

    // Dismiss cookies
    await page.evaluate(() => {
        const selectors = [
            'button[id*="accept"]', 'button[id*="Accept"]',
            'a[id*="accept"]', 'a[id*="Accept"]',
            '.cc-accept', '.cc-dismiss', 'button.accept-cookies',
        ];
        for (const sel of selectors) {
            const el = document.querySelector(sel);
            if (el) { el.click(); return; }
        }
        const buttons = document.querySelectorAll('button, a.btn, a[role="button"]');
        for (const btn of buttons) {
            const t = btn.textContent.trim().toLowerCase();
            if (['aceptar','accept','aceitar','accept all','aceptar todo','aceptar todas'].includes(t)) {
                btn.click(); return;
            }
        }
        document.querySelectorAll('[class*="cookie"], [class*="consent"], [id*="cookie"], [id*="consent"]')
            .forEach((el) => el.remove());
    }).catch(() => {});

    // Wait for reCAPTCHA (up to 3s)
    for (let i = 0; i < 20; i += 1) {
        const ready = await page.evaluate(
            () => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function',
        );
        if (ready) break;
        await sleep(150);
    }

    return { context, page, ready: true };
}

// --- Fill the pool up to MAX_CONCURRENT + 1 (buffer) ---
async function fillPool() {
    if (poolFilling) return;
    poolFilling = true;
    const poolTarget = MAX_CONCURRENT + 1;

    try {
        while (pagePool.length < poolTarget) {
            try {
                const entry = await createWarmedPage();
                pagePool.push(entry);
                fastify.log.info({ poolSize: pagePool.length }, 'Página pre-calentada añadida al pool');
            } catch (error) {
                fastify.log.warn({ err: error }, 'Error creando página pre-calentada');
                break;
            }
        }
    } finally {
        poolFilling = false;
    }
}

// --- Take a page from pool (or create one on demand) ---
async function acquirePage() {
    let entry = pagePool.shift();

    if (entry) {
        // Verify page is still alive
        try {
            await entry.page.evaluate(() => true);
            return entry;
        } catch {
            // Page died, close and fall through to create new
            try { await entry.context.close(); } catch {}
        }
    }

    // No pooled page available, create one on demand
    return createWarmedPage();
}

// --- Return a page to the pool by reloading it in background ---
function recyclePage(entry) {
    // Fire and forget: reload the page and put back in pool
    (async () => {
        try {
            await entry.page.goto(REDEEM_URL, { waitUntil: 'domcontentloaded', timeout: TIMEOUT_MS });
            await entry.page.waitForSelector('#pininput', { state: 'visible', timeout: TIMEOUT_MS });

            // Dismiss cookies again
            await entry.page.evaluate(() => {
                document.querySelectorAll('[class*="cookie"], [class*="consent"], [id*="cookie"], [id*="consent"]')
                    .forEach((el) => el.remove());
            }).catch(() => {});

            // Wait reCAPTCHA
            for (let i = 0; i < 20; i += 1) {
                const ready = await entry.page.evaluate(
                    () => typeof window.grecaptcha !== 'undefined' && typeof window.grecaptcha.execute === 'function',
                );
                if (ready) break;
                await sleep(150);
            }

            if (pagePool.length < MAX_CONCURRENT + 1) {
                pagePool.push(entry);
                fastify.log.info({ poolSize: pagePool.length }, 'Página reciclada al pool');
            } else {
                await entry.context.close();
            }
        } catch (error) {
            fastify.log.warn({ err: error }, 'Error reciclando página, descartada');
            try { await entry.context.close(); } catch {}
            // Replenish pool
            fillPool().catch(() => {});
        }
    })();
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
    let entry;
    let shouldRecycle = false;

    try {
        entry = await acquirePage();
        const { page } = entry;
        fastify.log.info({ elapsedMs: Date.now() - startedAt }, 'Página obtenida del pool');

        // --- PIN input via JS ---
        await page.waitForSelector('#pininput', { state: 'visible', timeout: TIMEOUT_MS });
        await page.evaluate((pin) => {
            const el = document.querySelector('#pininput');
            el.value = pin;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }, data.pin_key);

        // --- Wait for validate button to be enabled ---
        await page.waitForFunction(
            () => {
                const btn = document.querySelector('#btn-validate');
                return btn && !btn.disabled;
            },
            { timeout: TIMEOUT_MS, polling: 100 },
        );

        // --- Click validate via JS ---
        try {
            const validateResponsePromise = page.waitForResponse(
                (response) => response.url().includes('/validate') && !response.url().includes('account'),
                { timeout: TIMEOUT_MS },
            );
            await page.evaluate(() => document.querySelector('#btn-validate').click());
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
            shouldRecycle = true;
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
                shouldRecycle = true;
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

        // --- Country select via JS (avoids strict mode with duplicate IDs) ---
        await page.waitForFunction(
            () => {
                const sel = document.querySelector('#NationalityAlphaCode');
                return sel && sel.options.length > 1;
            },
            { timeout: 5000, polling: 100 },
        ).catch(() => {});

        const countryResult = await page.evaluate((countryNameLower) => {
            const sel = document.querySelector('#NationalityAlphaCode');
            if (!sel) return { selected: false, optionCount: 0 };

            const optionCount = sel.options.length;
            let targetValue = null;

            // Try user's country
            for (const opt of sel.options) {
                if (opt.text.toLowerCase().includes(countryNameLower)) {
                    targetValue = opt.value;
                    break;
                }
            }

            // Fallback: chile
            if (!targetValue) {
                for (const opt of sel.options) {
                    if (opt.text.toLowerCase().includes('chile')) {
                        targetValue = opt.value;
                        break;
                    }
                }
            }

            // Fallback: first non-empty
            if (!targetValue) {
                for (const opt of sel.options) {
                    if (opt.value) {
                        targetValue = opt.value;
                        break;
                    }
                }
            }

            if (targetValue) {
                sel.value = targetValue;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return { selected: true, targetValue, optionCount };
            }

            return { selected: false, optionCount };
        }, data.country.toLowerCase());

        fastify.log.info(countryResult, 'País procesado');

        let playerName = null;

        // --- Enable and click verify button via JS ---
        await page.evaluate(() => {
            document.querySelectorAll('#btn-verify, #btn-verify-account, .btn-verify')
                .forEach((btn) => btn.removeAttribute('disabled'));
        });

        const hasVerifyBtn = await page.evaluate(
            () => Boolean(document.querySelector('#btn-verify, #btn-verify-account, .btn-verify')),
        );

        if (hasVerifyBtn) {
            try {
                const accountResponsePromise = page.waitForResponse(
                    (response) => response.url().includes('validate/account'),
                    { timeout: 10_000 },
                );
                await page.evaluate(() => {
                    const btn = document.querySelector('#btn-verify') ||
                        document.querySelector('#btn-verify-account') ||
                        document.querySelector('.btn-verify');
                    if (btn) btn.click();
                });
                const accountResponse = await accountResponsePromise;
                const accountJson = await accountResponse.json();

                if (accountJson.Success) {
                    playerName = accountJson.Username || null;
                } else {
                    shouldRecycle = true;
                    return {
                        success: false,
                        message: 'Error de ID del jugador',
                        details: accountJson.Message || 'ID inválido',
                    };
                }
            } catch (error) {
                fastify.log.warn({ err: error }, 'No se pudo procesar validate/account');
            }
        } else {
            fastify.log.warn('Botón Verificar ID no encontrado, continuando');
        }

        // --- Check all checkboxes via JS ---
        await page.evaluate(() => {
            document.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
                cb.checked = true;
                cb.dispatchEvent(new Event('change', { bubbles: true }));
                cb.dispatchEvent(new Event('input', { bubbles: true }));
            });
        });

        // --- Clean overlays and prepare redeem button via JS ---
        await page.evaluate(() => {
            document.querySelectorAll(
                '[class*="cookie"], [class*="consent"], [id*="cookie"], [id*="consent"], ' +
                '[class*="Cookie"], [class*="Consent"], .cc-window, .cc-banner, #onetrust-banner-sdk',
            ).forEach((el) => el.remove());

            document.querySelectorAll('[class*="overlay"], [class*="backdrop"], [class*="modal"]').forEach((el) => {
                if (el.id !== 'btn-redeem' && !el.closest('.card')) {
                    el.remove();
                }
            });

            const btn = document.querySelector('#btn-redeem');
            if (btn) btn.removeAttribute('disabled');
        });

        const urlBefore = page.url();
        let confirmOk = false;
        let confirmBody = '';

        // --- Attempt 1: Click redeem via JS ---
        const hasRedeemBtn = await page.evaluate(() => {
            const btn = document.querySelector('#btn-redeem');
            return Boolean(btn);
        });

        if (hasRedeemBtn) {
            try {
                const confirmResponsePromise = page.waitForResponse(
                    (response) => response.url().includes('/confirm'),
                    { timeout: 10_000 },
                );
                await page.evaluate(() => document.querySelector('#btn-redeem').click());
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
            shouldRecycle = true;
            return {
                success: false,
                message: 'No se pudo enviar el formulario de canje',
                player_name: playerName,
                details: 'Ambos intentos principales de submit fallaron',
            };
        }

        // All post-submit paths can recycle the page
        shouldRecycle = true;

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
        if (entry) {
            if (shouldRecycle) {
                recyclePage(entry);
            } else {
                try { await entry.context.close(); } catch {}
                fillPool().catch(() => {});
            }
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
    pool_size: pagePool.length,
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
        pool_size: pagePool.length,
    };
});

fastify.addHook('onClose', async () => {
    // Close all pooled pages
    for (const entry of pagePool) {
        try { await entry.context.close(); } catch {}
    }
    pagePool.length = 0;
    await closeBrowser();
});

async function start() {
    try {
        await ensureBrowser();
        // Pre-warm page pool
        await fillPool();
        fastify.log.info({ poolSize: pagePool.length }, 'Pool de páginas pre-calentadas listo');
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