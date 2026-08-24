import asyncio
import json
import os
import re

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright, Error as PlaywrightError

HEADLESS = os.environ.get('HEADLESS', '0') == '1'
N8N_WEBHOOK_URL = os.environ.get('N8N_WEBHOOK_URL', 'https://dos8dosstudio.ddns.net/webhook/webhook')

AUDIO_PLAY_SEL = '[aria-label="Reproducir mensaje de voz"], [aria-label="Play voice message"]'

# WhatsApp Web dibuja su propio reproductor de audio (onda de sonido) en vez
# de usar un <audio> nativo, así que no hay ningún blob: URL que agarrar del
# DOM. En cambio, interceptamos decodeAudioData — la función del navegador
# que WhatsApp llama para decodificar el archivo antes de reproducirlo — y
# guardamos esos bytes (el archivo de audio real) en una variable global de
# la página, para leerla justo después de apretar play.
_SCRIPT_CAPTURA_AUDIO = """
(() => {
    function envolver(proto) {
        if (!proto || !proto.decodeAudioData || proto.__zwhatEnvuelto) return;
        const original = proto.decodeAudioData;
        proto.decodeAudioData = function (arrayBuffer, ...resto) {
            try {
                const bytes = new Uint8Array(arrayBuffer);
                let binario = '';
                for (let i = 0; i < bytes.length; i++) binario += String.fromCharCode(bytes[i]);
                window.__ultimoAudioCapturado = btoa(binario);
            } catch (e) {}
            return original.call(this, arrayBuffer, ...resto);
        };
        proto.__zwhatEnvuelto = true;
    }
    envolver(window.AudioContext && window.AudioContext.prototype);
    envolver(window.OfflineAudioContext && window.OfflineAudioContext.prototype);
})();
"""


async def _enviar_a_n8n(datos):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(N8N_WEBHOOK_URL, json=datos)
            print(f"  ↳ n8n respondió: {resp.status_code}")
    except Exception as e:
        print(f"  ⚠️ No se pudo avisar a n8n: {e}")


async def _extraer_audio_base64(page, burbuja):
    """Hace clic en 'play' y lee lo que la interceptación de decodeAudioData
    haya capturado — son los bytes reales del archivo de audio."""
    try:
        # DEBUG: ¿el navegador puede siquiera crear audio acá?
        estado_ctx = await page.evaluate(
            """() => {
                try {
                    const C = window.AudioContext || window.webkitAudioContext;
                    const ctx = new C();
                    return ctx.state;
                } catch (e) { return 'error: ' + e.message; }
            }"""
        )
        print(f"  🔍 DEBUG AudioContext: {estado_ctx}")

        await page.evaluate("window.__ultimoAudioCapturado = null;")

        boton = burbuja.locator(AUDIO_PLAY_SEL).first
        aria_antes = await boton.get_attribute('aria-label')

        peticiones = []

        def _on_request(req):
            if any(dom in req.url for dom in ('whatsapp.net', 'mmg', '.enc')):
                peticiones.append(req.url)

        page.on('request', _on_request)
        await boton.click(timeout=3000)
        await asyncio.sleep(2)
        page.remove_listener('request', _on_request)

        aria_despues = None
        try:
            aria_despues = await boton.get_attribute('aria-label')
        except Exception:
            pass
        print(f"  🔍 DEBUG aria-label del botón antes/después del click: {aria_antes!r} → {aria_despues!r}")
        print(f"  🔍 DEBUG peticiones de red relacionadas a medios: {peticiones}")

        b64 = None
        for _ in range(8):
            await asyncio.sleep(0.5)
            b64 = await page.evaluate("window.__ultimoAudioCapturado")
            if b64:
                break

        if not b64:
            total_audio_global = await page.locator('audio').count()
            print(f"  🔍 DEBUG <audio> en toda la página = {total_audio_global}")

        try:
            await boton.click(timeout=1000)  # pausar de nuevo
        except Exception:
            pass

        return b64
    except Exception as e:
        print(f"  ⚠️ No pude extraer el audio: {e}")
        return None

app = FastAPI(title="Zwhat - WhatsApp API")


class MensajeSaliente(BaseModel):
    numero: str
    mensaje: str


@app.post("/enviar-mensaje")
async def enviar_mensaje_endpoint(datos: MensajeSaliente, request: Request):
    """n8n llama acá con la respuesta de la IA. No se manda directo — se
    encola, porque solo hay UN navegador/sesión de WhatsApp compartido con
    el loop de monitoreo, y no pueden pelear por él al mismo tiempo."""
    await request.app.state.cola_envios.put({"numero": datos.numero, "mensaje": datos.mensaje})
    return {"status": "ok", "message": "Mensaje en cola para envío"}


@app.get("/qr")
async def ver_qr():
    """Para loguear por primera vez en un servidor sin pantalla: abrí esta
    URL en un navegador normal y refrescá cada pocos segundos hasta ver el
    QR y escanearlo."""
    if os.path.exists('qr.png'):
        return FileResponse('qr.png')
    return PlainTextResponse("Sin QR pendiente (todavía no cargó o ya hay sesión iniciada)")


class NumeroChat(BaseModel):
    numero: str


@app.post("/pausar-chat")
async def pausar_chat(datos: NumeroChat, request: Request):
    """n8n llama acá cuando la IA detecta que el cliente pidió un humano.
    Mientras esté pausado, el loop de monitoreo salta ese número por
    completo (ni lo abre ni le manda nada a n8n) — queda libre para que
    alguien conteste a mano desde WhatsApp normal."""
    request.app.state.pausados[datos.numero] = True
    print(f"  ⏸️ {datos.numero} pausado — no se va a procesar hasta que se reanude")
    return {"status": "ok", "pausado": True}


@app.post("/reanudar-chat")
async def reanudar_chat(datos: NumeroChat, request: Request):
    """Para devolver un número al bot — llamalo desde n8n cuando el humano
    termine, o después de tu propio timeout (ej. nodo Wait de n8n) si
    querés mandar un 'el asesor está ocupado' y seguir con el bot."""
    request.app.state.pausados.pop(datos.numero, None)
    print(f"  ▶️ {datos.numero} reanudado — vuelve al bot")
    return {"status": "ok", "pausado": False}


@app.get("/estado-chat/{numero}")
async def estado_chat(numero: str, request: Request):
    """Para que n8n chequee, antes de mandar el mensaje de timeout, si
    el número sigue pausado (por si un humano ya contestó y no querés
    mandar el aviso de 'asesor ocupado' de todas formas)."""
    return {"numero": numero, "pausado": numero in request.app.state.pausados}


HORA_RE = re.compile(r'\d{1,2}:\d{2}\s*([ap]\.?\s*m\.?)?', re.IGNORECASE)


def _es_timestamp(linea):
    return bool(HORA_RE.fullmatch(linea.strip()))


def _es_saliente(data_id):
    """Confirmado con datos reales: los data-id de mensajes que salen desde
    acá empiezan con '3EB0' (formato de ID que genera el propio cliente al
    componer un mensaje); los del contacto son un hash largo (ej. 'AC...')."""
    return bool(data_id) and data_id.startswith('3EB0')


# ── Debounce genérico: esperar 10s de calma antes de seguir ──────────────────

async def _esperar_estable(obtener_valor, espera=2.0, quieto_necesario=10.0, tope=60.0):
    """Sondea obtener_valor() cada `espera` segundos. Si cambia, reinicia el
    contador de calma. Termina cuando pasan `quieto_necesario` segundos
    seguidos sin cambios (o al llegar al `tope`), y devuelve el último valor
    visto."""
    anterior = await obtener_valor()
    quieto = 0.0
    transcurrido = 0.0
    while quieto < quieto_necesario and transcurrido < tope:
        await asyncio.sleep(espera)
        transcurrido += espera
        actual = await obtener_valor()
        if actual == anterior:
            quieto += espera
        else:
            print(f"  ⏳ Sigue escribiendo/llegando mensajes ({anterior} → {actual}), reiniciando espera de 10s")
            quieto = 0.0
            anterior = actual
    return anterior


async def _badge_texto(container):
    try:
        return await container.locator('[data-testid="icon-unread-count"]').first.inner_text(timeout=1000)
    except Exception:
        return None


async def _esperar_badge_estable(container):
    """Afuera del chat: espera 10s de calma en el badge y devuelve la
    cantidad final de mensajes no leídos (para saber cuántos traer)."""
    texto_final = await _esperar_estable(lambda: _badge_texto(container))
    solo_dig = ''.join(c for c in (texto_final or '') if c.isdigit())
    return int(solo_dig) if solo_dig else 1


async def _contar_entrantes(page):
    """Cuenta solo mensajes entrantes (data-id que NO empieza con '3EB0') —
    una respuesta nuestra no debe contar como 'llegó algo nuevo del cliente'."""
    ids = await page.locator('#main [data-id]').evaluate_all(
        "els => els.map(e => e.getAttribute('data-id'))"
    )
    return sum(1 for d in ids if not _es_saliente(d))


async def _contar_salientes(page):
    """Cuenta mensajes salientes (nuestras respuestas) — sirve para detectar
    que ya se respondió y hay que dejar de esperar en este chat."""
    ids = await page.locator('#main [data-id]').evaluate_all(
        "els => els.map(e => e.getAttribute('data-id'))"
    )
    return sum(1 for d in ids if _es_saliente(d))


async def _esperar_mensajes_estables(page):
    """Adentro del chat: espera 10s de calma en la cantidad de mensajes
    ENTRANTES (ignora lo que nosotros mandemos mientras tanto)."""
    await _esperar_estable(lambda: _contar_entrantes(page))


# ── PASO 1: Abrir chat no leído (saltea Archivados) ──────────────────────────

async def abrir_chat_no_leido(page, info_cache, pausados):
    """Devuelve (cantidad, titulo_lista). cantidad=0 si no hay nada para
    abrir. titulo_lista es el nombre tal como aparece en la lista de chats —
    sirve como clave de caché antes de siquiera abrir el panel de info."""
    badges = page.locator('#pane-side [data-testid="icon-unread-count"]')
    total = await badges.count()
    if total == 0:
        return 0, None

    print(f"\n📬 {total} chat(s) con mensajes no leídos")

    for i in range(total):
        container = badges.nth(i).locator(
            'xpath=ancestor-or-self::*[@data-testid="cell-frame-container"]'
        ).first

        try:
            # Señal estructural: la fila de la carpeta "Archivados" tiene un
            # ícono de archivo en vez de foto de perfil — no depende del
            # idioma ni de que el texto matchee exacto.
            if await container.locator('[data-icon="archive"]').count() > 0:
                print("  ↳ Fila de Archivados detectada (ícono), salteando")
                continue
        except Exception:
            pass

        titulo_lista = None
        try:
            titulo_lista = (await container.locator('span[dir="auto"]').first.inner_text(timeout=1000)).strip()
        except Exception:
            pass

        # Si ya conocemos el número de este chat (por info_cache) y está
        # pausado (pidió un humano), lo salteamos por completo.
        info_previa = info_cache.get(titulo_lista) if titulo_lista else None
        if info_previa and info_previa[1] in pausados:
            print(f"  ⏸️ {titulo_lista} ({info_previa[1]}) está pausado, salteando")
            continue

        # Debounce afuera: esperar a que el badge de ESTE chat deje de subir
        # antes de abrirlo (10s de calma), y quedarnos con el número final.
        cantidad = await _esperar_badge_estable(container)

        try:
            # force=True: en chats con emoji en el título, el span del emoji
            # a veces intercepta el click normal y lo hace fallar
            await container.click(force=True, timeout=5000)
            print("  ↳ Click ejecutado")
        except Exception as e:
            print(f"  ⚠️ Click: {e}")
            continue

        try:
            await page.wait_for_selector('#main [data-id]', timeout=10000)
            await asyncio.sleep(1)
        except Exception:
            print("  ⚠️ El chat no cargó en 10s")
            return 0, titulo_lista

        # Debounce adentro: por si la persona sigue escribiendo con el chat
        # ya abierto (ahí el badge no sirve porque se marca leído al toque).
        # Lo que crezca acá se suma a lo que ya contaba el badge. Contamos
        # solo entrantes: si alguien responde desde acá mientras esperamos,
        # no debe sumar como "mensaje nuevo del cliente".
        total_al_abrir = await _contar_entrantes(page)
        await _esperar_mensajes_estables(page)
        total_estable = await _contar_entrantes(page)
        cantidad += max(0, total_estable - total_al_abrir)

        return cantidad, titulo_lista

    print("  ↳ Solo hay Archivados, sin chats nuevos")
    return 0, None


# ── PASO 2: Nombre y número desde el panel de info del contacto ──────────────
# Se leen los dos juntos de una sola apertura del panel: el título del header
# cambia de selector seguido, pero dentro del panel "Info. del contacto" la
# línea justo antes del número siempre es el nombre.

async def extraer_info_contacto(page):
    # Nota: sin Escape acá al principio — si la ventana está angosta,
    # WhatsApp Web usa el layout de una sola columna y Escape ahí significa
    # "volver a la lista de chats", cerrando el chat recién abierto.

    # Esperar a que el header del chat termine de renderizar antes de intentar
    # cualquier click — si el chat recién se abrió, el locator puede no existir
    # todavía y el click ni siquiera llega a intentarse (timeout "waiting for
    # locator", no "click bloqueado").
    try:
        await page.wait_for_selector('#main header', timeout=8000)
        print("  ↳ #main header apareció")
    except Exception:
        print("  ⚠️ #main header no apareció en 8s — el chat no terminó de cargar")
        return None, None

    await asyncio.sleep(0.5)

    # Intento 1: botón específico de "Información del perfil" por aria-label/title
    # (más estable que la clase del header, que WhatsApp Web reescribe seguido).
    clic_ok = False
    for sel in [
        '[aria-label="Información del perfil"]',
        '[title="Información del perfil"]',
        '[aria-label="Profile info"]',
        '[title="Profile info"]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=2000)
            print(f"  ↳ Click OK con selector: {sel}")
            clic_ok = True
            break
        except Exception:
            pass

    # Intento 2 (fallback): click forzado sobre el header entero
    if not clic_ok:
        try:
            await page.locator('#main header').first.click(force=True, timeout=5000)
            print("  ↳ Click OK con fallback: #main header")
            clic_ok = True
        except Exception as e:
            print(f"  ⚠️ Click header (fallback): {e}")
            return None, None

    panel_sel = None
    for sel in ['[data-testid="drawer-right"]', '#app aside', 'div[tabindex="-1"] section']:
        try:
            await page.wait_for_selector(sel, timeout=3000)
            panel_sel = sel
            print(f"  ↳ Panel encontrado con selector: {sel}")
            break
        except Exception:
            print(f"  ↳ Panel NO encontrado con selector: {sel}")

    if panel_sel is None:
        print("  ⚠️ Panel de info no apareció con ningún selector")
        try:
            html = await page.locator('#app').first.inner_html(timeout=2000)
            with open('debug_panel.html', 'w', encoding='utf-8') as f:
                f.write(html)
            print("  🔍 HTML de #app volcado en debug_panel.html")
        except Exception:
            pass
        await page.keyboard.press('Escape')
        return None, None

    await asyncio.sleep(1.2)

    nombre = None
    numero = None
    try:
        texto = await page.locator(panel_sel).first.inner_text(timeout=3000)
        lineas = [l.strip() for l in texto.split('\n') if l.strip()]
        print(f"  DEBUG drawer completo ({len(lineas)} líneas):")
        for idx, l in enumerate(lineas):
            print(f"    [{idx}] {l}")

        for idx, linea in enumerate(lineas):
            solo_dig = ''.join(c for c in linea if c.isdigit())
            if 7 <= len(solo_dig) <= 15 and (linea.startswith('+') or linea[0].isdigit()):
                numero = solo_dig
                if idx > 0:
                    nombre = lineas[idx - 1]
                print(f"  ↳ Nombre: {nombre} / Número: {numero}")
                break
    except Exception as e:
        print(f"  ⚠️ Leyendo drawer: {e}")

    await page.keyboard.press('Escape')
    await asyncio.sleep(0.5)
    return nombre, numero


# ── PASO 3: Últimos N mensajes ENTRANTES, unidos en un solo texto ────────────
# N viene del badge de WhatsApp, pero es solo un límite de seguridad — el
# corte real es `ultimo_id`: el data-id del último mensaje que ya mandamos
# a n8n antes para este número. Así, aunque el badge muestre un backlog
# grande (por pruebas viejas, reinicios, etc.), nunca repetimos lo que ya
# procesamos. Devuelve (mensaje, nuevo_ultimo_id, audios_base64).

async def extraer_mensaje(page, cantidad, ultimo_id=None):
    try:
        msgs = page.locator('#main [data-id]')
        total = await msgs.count()
        if total == 0:
            return None, ultimo_id, []

        nuevo_ultimo_id = ultimo_id
        primer_entrante = True

        textos = []
        audios = []
        restantes = max(1, cantidad)
        i = total - 1
        tope = max(total - 60, -1)  # límite de seguridad, no escanear sin fin

        while restantes > 0 and i > tope:
            el = msgs.nth(i)
            data_id = await el.get_attribute('data-id') or ''

            if data_id == ultimo_id:
                print(f"  ↳ msg[{i}] ya procesado antes (ultimo_id), corte de ráfaga")
                break

            if _es_saliente(data_id):
                print(f"  ↳ msg[{i}] data-id={data_id} → saliente, se saltea")
                i -= 1
                continue

            if primer_entrante:
                nuevo_ultimo_id = data_id
                primer_entrante = False

            txt = await el.inner_text(timeout=2000)
            encontro_texto = False
            for linea in (l.strip() for l in txt.split('\n') if l.strip()):
                if _es_timestamp(linea) or len(linea) <= 1:
                    continue
                textos.append(linea)
                encontro_texto = True
                break

            if not encontro_texto:
                es_audio = await el.locator(AUDIO_PLAY_SEL).count() > 0
                if es_audio:
                    print(f"  🎤 msg[{i}] es una nota de voz, extrayendo audio...")
                    b64 = await _extraer_audio_base64(page, el)
                    if b64:
                        print(f"  ↳ Audio capturado ({len(b64)} chars base64)")
                        audios.append(b64)
                        textos.append("[nota de voz adjunta]")
                    else:
                        print("  ⚠️ No se pudo capturar el audio")
                        textos.append("[nota de voz, no se pudo capturar]")
                else:
                    # Otro tipo sin texto (imagen/sticker/etc.) — volcamos el
                    # HTML de la burbuja para poder identificarlo más adelante.
                    try:
                        html_bubble = await el.inner_html(timeout=1500)
                        print(f"  🔍 msg[{i}] sin texto (¿imagen/sticker?) data-id={data_id}")
                        print(f"     HTML: {html_bubble[:1500]}")
                    except Exception:
                        pass

            restantes -= 1
            i -= 1

        textos.reverse()
        mensaje = '\n'.join(textos) if textos else None
        return mensaje, nuevo_ultimo_id, audios
    except Exception as e:
        print(f"  ⚠️ Mensaje: {e}")
        return None, ultimo_id, []


# ── Envío de mensajes salientes ───────────────────────────────────────────────
# Buscador confirmado en vivo: es un <input> real (no contenteditable) con
# aria-label="Buscar un chat o iniciar uno nuevo". La caja de texto del chat
# (para escribir el mensaje) todavía no está confirmada.

async def enviar_mensaje_a_chat(page, numero, mensaje):
    try:
        buscador = None
        for sel in [
            '[aria-label="Buscar un chat o iniciar uno nuevo"]',
            '[data-testid="chat-list-search-container"] input',
            '[aria-label="Buscar o empezar un chat nuevo"]',
        ]:
            try:
                await page.locator(sel).first.click(timeout=2000)
                buscador = sel
                break
            except Exception:
                pass

        if buscador is None:
            print("  ⚠️ No encontré el buscador de chats — no se pudo enviar")
            try:
                # #pane-side es solo la lista — el buscador vive en un
                # contenedor hermano por arriba, así que volcamos #app entero.
                html = await page.locator('#app').first.inner_html(timeout=2000)
                with open('debug_app.html', 'w', encoding='utf-8') as f:
                    f.write(html)
                print("  🔍 HTML de #app volcado en debug_app.html")
            except Exception as e2:
                print(f"  ⚠️ No pude volcar #app: {e2}")
            return False
        print(f"  ↳ Buscador OK con selector: {buscador}")

        # Limpiar cualquier texto que haya quedado de una búsqueda anterior
        # — si no, se concatena con el número nuevo y no matchea nada.
        await page.keyboard.press('Control+A')
        await page.keyboard.press('Backspace')
        await page.keyboard.type(numero, delay=30)
        await asyncio.sleep(1.5)

        try:
            await page.locator('#pane-side [data-testid="cell-frame-container"]').first.click(timeout=5000)
        except Exception as e:
            print(f"  ⚠️ No encontré resultado de búsqueda para {numero}: {e}")
            await page.keyboard.press('Escape')
            return False

        await page.wait_for_selector('#main [data-id]', timeout=10000)
        await asyncio.sleep(1)

        caja = None
        for sel in [
            '[aria-label="Escribe un mensaje"]',
            '#main footer [contenteditable="true"]',
            '#main [contenteditable="true"][data-tab]',
        ]:
            try:
                await page.locator(sel).first.click(timeout=2000)
                caja = sel
                break
            except Exception:
                pass

        if caja is None:
            print("  ⚠️ No encontré la caja de texto — no se pudo enviar")
            return False
        print(f"  ↳ Caja de texto OK con selector: {caja}")

        salientes_antes = await _contar_salientes(page)
        await page.keyboard.type(mensaje, delay=15)
        await page.keyboard.press('Enter')

        # No confiamos en solo apretar Enter — confirmamos que realmente
        # apareció un mensaje saliente nuevo antes de dar por enviado.
        confirmado = False
        for _ in range(6):
            await asyncio.sleep(0.5)
            if await _contar_salientes(page) > salientes_antes:
                confirmado = True
                break

        if not confirmado:
            print(f"  ⚠️ Se tipeó y apretó Enter pero no se confirmó el envío a {numero}")
            return False

        print(f"  ✅ Mensaje enviado a {numero} (confirmado)")
        return True
    except Exception as e:
        print(f"  ⚠️ Error enviando mensaje a {numero}: {e}")
        return False
    finally:
        # Dejar todo limpio (cerrar búsqueda / deseleccionar chat) para que
        # el próximo envío o el loop de monitoreo arranquen de cero.
        try:
            await page.keyboard.press('Escape')
        except Exception:
            pass


async def _procesar_cola_envios(page, cola, intentos_max=10):
    while True:
        item = await cola.get()
        print(f"\n📤 Enviando a {item['numero']!r}: {item['mensaje']!r}")

        for intento in range(1, intentos_max + 1):
            if await enviar_mensaje_a_chat(page, item['numero'], item['mensaje']):
                break
            if intento < intentos_max:
                print(f"  🔁 No se pudo enviar (intento {intento}/{intentos_max}), reintentando...")
                await asyncio.sleep(2)
        else:
            print(f"  ❌ No se pudo enviar a {item['numero']} después de {intentos_max} intentos, se descarta")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    info_cache = {}  # titulo_lista -> (nombre, numero), para no reabrir el panel de info
    ultimo_id_por_numero = {}  # numero -> último data-id ya mandado a n8n
    pausados = {}  # numero -> True mientras esté pausado (pidió un humano)

    async with async_playwright() as p:
        print("🚀 Lanzando navegador...")

        browser = await p.chromium.launch_persistent_context(
            user_data_dir="./wa_session",
            headless=HEADLESS,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.add_init_script(_SCRIPT_CAPTURA_AUDIO)

        print("🌐 Cargando WhatsApp Web...")
        await page.goto("https://web.whatsapp.com/")

        # Levantamos la API YA, antes de esperar el login — así /qr está
        # disponible para loguear por primera vez en un servidor sin pantalla.
        app.state.cola_envios = asyncio.Queue()
        app.state.pausados = pausados
        puerto = int(os.environ.get('PORT', '8000'))
        config = uvicorn.Config(app, host="0.0.0.0", port=puerto, log_level="warning")
        server = uvicorn.Server(config)
        server_task = asyncio.create_task(server.serve())
        print(f"🌐 API escuchando en :{puerto} (POST /enviar-mensaje, GET /qr)")

        logueado = False
        for intento in range(90):  # ~7-8 min de margen para escanear
            try:
                await page.wait_for_selector('#pane-side', timeout=5000)
                logueado = True
                break
            except Exception:
                pass
            try:
                await page.screenshot(path='qr.png')
            except Exception:
                pass
            if intento == 0:
                print(f"📷 Falta loguear WhatsApp — abrí http://<IP-DEL-VPS>:{puerto}/qr en un navegador y escaneá el QR (se actualiza solo)")

        if not logueado:
            print("❌ No se logueó a tiempo, terminando")
            server_task.cancel()
            await browser.close()
            return

        if os.path.exists('qr.png'):
            os.remove('qr.png')

        await page.keyboard.press('Escape')
        await asyncio.sleep(0.5)
        print("✅ Sesión lista — vigilando mensajes nuevos...")

        try:
            await asyncio.gather(
                server_task,
                _monitorear(page, info_cache, ultimo_id_por_numero, pausados),
                _procesar_cola_envios(page, app.state.cola_envios),
            )
        except PlaywrightError:
            print("\n🛑 El navegador se cerró — terminando prolijamente.")
        except KeyboardInterrupt:
            print("\n🛑 Interrumpido por el usuario.")


async def _monitorear(page, info_cache, ultimo_id_por_numero, pausados):
    while True:
        cantidad, titulo_lista = await abrir_chat_no_leido(page, info_cache, pausados)
        if cantidad:
            print("  ✅ PASO 1 (abrir chat): OK")

            if titulo_lista in info_cache:
                nombre, numero = info_cache[titulo_lista]
                print(f"  ↳ Info de contacto en caché: nombre={nombre!r} numero={numero!r}")
            else:
                nombre, numero = await extraer_info_contacto(page)
                print(f"  ✅ PASO 2 (info contacto): nombre={nombre!r} numero={numero!r}")
                if titulo_lista and nombre and numero:
                    info_cache[titulo_lista] = (nombre, numero)

            ultimo_id = ultimo_id_por_numero.get(numero)

            # Nos quedamos procesando ESTE chat hasta que de verdad no
            # llegue nada más, O hasta que se mande una respuesta desde
            # acá — ahí cortamos y volvemos a esperar mensajes nuevos en
            # vez de seguir esperando calma en este mismo chat.
            while True:
                mensaje, ultimo_id, audios = await extraer_mensaje(page, cantidad, ultimo_id)
                if numero:
                    ultimo_id_por_numero[numero] = ultimo_id
                print(f"  ✅ PASO 3 (mensaje, {cantidad} nuevo(s)): {mensaje!r} ({len(audios)} audio(s))")

                if mensaje is not None:
                    datos = {"nombre": nombre, "numero": numero, "mensaje": mensaje, "audios": audios}
                    print("=" * 50)
                    print(json.dumps(
                        {**datos, "audios": [f"<{len(a)} chars base64>" for a in audios]},
                        ensure_ascii=False, indent=2
                    ))
                    print("=" * 50)
                    await _enviar_a_n8n(datos)

                entrantes_antes = await _contar_entrantes(page)
                salientes_antes = await _contar_salientes(page)
                await _esperar_mensajes_estables(page)
                entrantes_despues = await _contar_entrantes(page)
                salientes_despues = await _contar_salientes(page)

                if salientes_despues > salientes_antes:
                    print("  ↳ Se envió una respuesta — salimos del chat y volvemos a esperar mensajes nuevos")
                    break

                cantidad = entrantes_despues - entrantes_antes
                if cantidad <= 0:
                    break
                print(f"  ⏳ Llegaron {cantidad} mensaje(s) más justo al terminar, reprocesando...")

            # Deseleccionar el chat: si lo dejamos abierto/enfocado, WhatsApp
            # marca como leído cualquier mensaje nuevo al instante y nunca
            # aparece el badge de no leído — así el próximo mensaje de este
            # mismo contacto sí dispara la detección normal.
            try:
                await page.keyboard.press('Escape')
            except Exception:
                pass
            await asyncio.sleep(0.5)

        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
