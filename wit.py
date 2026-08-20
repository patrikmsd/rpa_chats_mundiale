from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import psycopg2
import time
import json
import os
import re
import sys
from datetime import datetime, date

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

from config import PG, WITHUB_USERNAME, WITHUB_PASSWORD

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "wit_progress.json")

TABELA = "public.historico_atendimentos"


def conectar_pg():
    return psycopg2.connect(**PG)


# ── helpers de banco ──────────────────────────────────────────────────────────

def parsear_hora_msg(raw):
    """
    Converte o texto do timestamp da última mensagem em (data_str, hora_str).
    Aceita "HH:MM" (assume hoje) ou "DD/MM/AAAA HH:MM".
    """
    if not raw:
        return "", ""
    raw = raw.strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            pass
    # apenas hora
    try:
        datetime.strptime(raw, "%H:%M")
        return date.today().strftime("%Y-%m-%d"), raw
    except ValueError:
        return "", raw


def parsear_tempo(raw):
    """
    Converte string de tempo do WIT para total de segundos (int).
    Formatos: "2 dias 23:52:53", "1 dia 01:23:45", "HH:MM:SS", "MM:SS"
    Retorna None se não conseguir parsear.
    """
    if not raw:
        return None
    raw = raw.strip()
    dias = 0
    m = re.match(r'(\d+)\s+dias?\s+(\d+):(\d+):(\d+)', raw)
    if m:
        dias, h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return dias * 86400 + h * 3600 + mi * 60 + s
    m = re.match(r'(\d+):(\d+):(\d+)', raw)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return h * 3600 + mi * 60 + s
    m = re.match(r'(\d+):(\d+)', raw)
    if m:
        mi, s = int(m.group(1)), int(m.group(2))
        return mi * 60 + s
    return None


def nome_remetente(r):
    """Retorna o nome de quem enviou a última mensagem."""
    if r["ultima_msg_de"] == "agente":
        return r["atendente"]
    return r["cliente"] or r["contato"]


def normalizar_empresa(nome):
    """'Ancore Atendimento' → 'ancore', 'BR TRUCK Atendimento' → 'br truck'"""
    return re.sub(r'\s*Atendimento\s*$', '', nome, flags=re.IGNORECASE).strip().lower()


def carregar_progresso():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"empresa_idx": 0}


def salvar_progresso(empresa_idx):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({"empresa_idx": empresa_idx}, f)


def limpar_progresso():
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)


def salvar(conn, registros, empresa):
    for r in registros:
        try:
            cur = conn.cursor()
            msg_data, msg_hora = parsear_hora_msg(r["ultima_msg_hora"])
            agora = datetime.now()

            tempo_segundos = parsear_tempo(r["tempo_atend"])
            cur.execute(f"SELECT id FROM {TABELA} WHERE protocolo_id = %s", (r["ticket"],))
            existe = cur.fetchone()

            if existe:
                cur.execute(f"""
                    UPDATE {TABELA} SET
                        numero_cliente        = %s,
                        nome_cliente          = %s,
                        nome_agente           = %s,
                        departamento_agente   = %s,
                        ultima_mensagem_nome  = %s,
                        ultima_mensagem_data  = %s,
                        ultima_mensagem_hora  = %s,
                        status                = 'Em atendimento',
                        tempo_de_espera       = %s,
                        em_espera             = %s,
                        chat                  = 'mundiale',
                        empresa               = %s
                    WHERE protocolo_id = %s
                """, (
                    r["contato"], r["cliente"], r["atendente"], r["fila"],
                    nome_remetente(r), msg_data, msg_hora,
                    tempo_segundos, r["em_espera"],
                    empresa, r["ticket"],
                ))
            else:
                cur.execute(f"""
                    INSERT INTO {TABELA}
                        (empresa, data_criado, hora_criado, protocolo_id,
                         numero_cliente, nome_cliente, nome_agente, departamento_agente,
                         ultima_mensagem_nome, ultima_mensagem_data, ultima_mensagem_hora,
                         status, tempo_de_espera, em_espera, chat)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Em atendimento', %s, %s, 'mundiale')
                """, (
                    empresa,
                    agora.strftime("%Y-%m-%d"), agora.strftime("%H:%M"),
                    r["ticket"],
                    r["contato"], r["cliente"], r["atendente"], r["fila"],
                    nome_remetente(r), msg_data, msg_hora,
                    tempo_segundos, r["em_espera"],
                ))

            conn.commit()

        except Exception as e:
            conn.rollback()  # limpa a transação abortada para não travar os próximos
            print(f"  ERRO ao salvar ticket {r.get('ticket')}: {e}")


def finalizar_ausentes(conn, tickets_ativos):
    """
    Tickets que estavam 'Em atendimento' mas não aparecem mais na coleta
    são marcados como 'Finalizado'.
    """
    if not tickets_ativos:
        return
    cur = conn.cursor()
    cur.execute(f"""
        UPDATE {TABELA}
        SET status = 'Finalizado', em_espera = FALSE
        WHERE status = 'Em atendimento'
          AND protocolo_id NOT IN %s
    """, (tuple(tickets_ativos),))
    finalizados = cur.rowcount
    conn.commit()
    if finalizados:
        print(f"  {finalizados} atendimento(s) marcado(s) como Finalizado.")


# ── helpers de extração ───────────────────────────────────────────────────────

def _txt(el):
    try:
        return el.inner_text(timeout=2000).strip()
    except Exception:
        return ""


def _aria(el):
    try:
        return el.get_attribute("aria-label", timeout=2000) or ""
    except Exception:
        return ""


def ultima_mensagem(page):
    """
    Retorna (texto, remetente, horario) da última mensagem aberta no painel.
    Funciona para mensagens de agente e de cliente.
    Horário vem do <span> filho direto do wrapper .css-1fy0vq2 (irmão do bubble div).
    """
    try:
        resultado = page.evaluate("""
            () => {
                const wrappers = document.querySelectorAll('.css-1fy0vq2');
                if (!wrappers.length) return ['', '', ''];
                const last = wrappers[wrappers.length - 1];

                // Texto: primeiro span.MuiTypography-caption dentro do bubble
                const textSpan = last.querySelector('span.MuiTypography-caption');
                const texto = textSpan ? textSpan.innerText.trim() : '';

                // Remetente: o bubble é o primeiro div filho do wrapper
                // css-1bvc4cc = agente (alinhado à direita), outro = cliente
                const bubble = last.querySelector(':scope > div.MuiBox-root');
                const remetente = (bubble && bubble.className.includes('css-1bvc4cc'))
                    ? 'agente' : 'cliente';

                // Horário: o span filho DIRETO do wrapper (irmão do bubble div)
                // Estrutura: wrapper > div(bubble) + span(timestamp container)
                //            timestamp container > span(hora) + svg(icone lido)
                const timeContainer = Array.from(last.children)
                    .find(c => c.tagName === 'SPAN');
                const timeSpan = timeContainer
                    ? timeContainer.querySelector('span')
                    : null;
                const horario = timeSpan ? timeSpan.innerText.trim() : '';

                return [texto, remetente, horario];
            }
        """)
        return tuple(resultado) if resultado else ("", "", "")
    except Exception:
        return "", "", ""


# ── login e navegação (extraído de wit.py) ────────────────────────────────────

def login_e_navegar(browser):
    page = browser.new_page()
    page.goto("https://account.withub.ai/login")
    page.fill("#username", WITHUB_USERNAME)
    page.fill("#password", WITHUB_PASSWORD)
    page.click("button[name='login']")
    page.get_by_role("button", name="Acessar").first.click()
    page.wait_for_timeout(5000)

    # lida com possível abertura de nova aba
    pages = browser.contexts[0].pages
    target = pages[-1] if len(pages) > 1 else page
    if len(pages) > 1:
        print("Nova aba detectada, usando ela.")

    target.locator("span", has_text="speed").first.click()
    target.get_by_role("tab", name="Em Atendimento").click()
    target.wait_for_timeout(2000)

    return target


# ── coleta da página atual ────────────────────────────────────────────────────

def _indices_visiveis(page):
    """Retorna os aria-rowindex presentes no DOM — um único round-trip JS."""
    return page.evaluate("""
        () => Array.from(document.querySelectorAll('[role="row"][aria-rowindex]'))
            .map(r => parseInt(r.getAttribute('aria-rowindex')))
            .filter(n => n > 1)
            .sort((a, b) => a - b)
    """)


def _extrair_linha(page, idx):
    """
    Scrolla até a linha, aguarda o React renderizar o conteúdo (wait_for_function
    retorna assim que a célula tiver texto — muito mais rápido que sleep fixo),
    e extrai todos os campos em um único evaluate.
    """
    # Passo 1: scroll
    page.evaluate(f"""
        () => {{
            const s = document.querySelector('.MuiDataGrid-virtualScroller');
            const r = document.querySelector('[role="row"][aria-rowindex="{idx}"]');
            if (s && r) s.scrollTop = r.offsetTop - s.offsetTop;
        }}
    """)

    # Passo 2: aguarda célula ter conteúdo (retorna no momento em que aparecer)
    try:
        page.wait_for_function(
            f"""() => {{
                const el = document.querySelector(
                    '[role="row"][aria-rowindex="{idx}"] [data-field="ticket"] .MuiDataGrid-cellContent'
                );
                return el && el.innerText.trim().length > 0;
            }}""",
            timeout=2000,
        )
    except PWTimeout:
        pass  # linha vazia — extrai o que tiver

    # Passo 3: extrai
    return page.evaluate(f"""
        () => {{
            const row = document.querySelector('[role="row"][aria-rowindex="{idx}"]');
            if (!row) return null;
            const txt  = s => {{ const e = row.querySelector(s); return e ? e.innerText.trim() : ''; }};
            const aria = s => {{ const e = row.querySelector(s); return e ? (e.getAttribute('aria-label') || e.innerText.trim()) : ''; }};
            return {{
                ticket:      txt('[data-field="ticket"] .MuiDataGrid-cellContent'),
                contato:     aria('[data-field="contactOrigin"] span'),
                fila:        txt('[data-field="queue"] .MuiChip-label'),
                atendente:   aria('[data-field="attendant"] span'),
                cliente:     aria('[data-field="client"] span'),
                tempo_atend: txt('[data-field="attendanceTime"]'),
            }};
        }}
    """)


def coletar_pagina(page, conn, empresa):
    # Grid já está pronto (ir_para_proxima ou main garantiu isso)

    indices = _indices_visiveis(page)
    if not indices:
        print("  0 linhas nesta página — sem atendimentos.")
        return []
    print(f"  {len(indices)} linhas nesta página (índices {indices[0]}–{indices[-1]})")

    tickets_pagina = []

    for idx in indices:
        try:
            dados = _extrair_linha(page, idx)

            if not dados:
                print(f"  [{idx}] linha não encontrada, pulando")
                continue

            # ── abre o chat e lê a última mensagem + horário ─────────────────
            msg_texto, msg_de, msg_hora = "", "", ""
            chat_btn = page.query_selector(
                f'[role="row"][aria-rowindex="{idx}"] [data-field="action"] [aria-label="Monitorar chat"]'
            )
            if chat_btn:
                chat_btn.click()
                try:
                    page.wait_for_selector(".css-1fy0vq2", timeout=3000)
                    # Espera condicional em vez de sleep fixo: retorna assim que o
                    # texto da última mensagem realmente estiver renderizado.
                    try:
                        page.wait_for_function(
                            """() => {
                                const wrappers = document.querySelectorAll('.css-1fy0vq2');
                                if (!wrappers.length) return false;
                                const last = wrappers[wrappers.length - 1];
                                const span = last.querySelector('span.MuiTypography-caption');
                                return span && span.innerText.trim().length > 0;
                            }""",
                            timeout=1500,
                        )
                    except PWTimeout:
                        pass
                    msg_texto, msg_de, msg_hora = ultima_mensagem(page)
                except PWTimeout:
                    pass

                close = (
                    page.query_selector('[aria-label="Fechar chat"]') or
                    page.query_selector('[aria-label="Fechar"]') or
                    page.query_selector('[data-testid="CloseIcon"]:not([class*="Autocomplete"])')
                )
                if close:
                    close.click()
                else:
                    page.keyboard.press("Escape")
                # Espera o painel de chat realmente sumir em vez de sleep fixo.
                try:
                    page.wait_for_function(
                        "() => document.querySelectorAll('.css-1fy0vq2').length === 0",
                        timeout=800,
                    )
                except PWTimeout:
                    pass

            registro = {
                "ticket":          dados["ticket"],
                "contato":         dados["contato"],
                "fila":            dados["fila"],
                "atendente":       dados["atendente"],
                "cliente":         dados["cliente"],
                "tempo_atend":     dados["tempo_atend"],
                "ultima_msg":      msg_texto,
                "ultima_msg_de":   msg_de,
                "ultima_msg_hora": msg_hora,
                "em_espera":       msg_de == "cliente",  # sistema/agente → False

            }

            # ── salva imediatamente após coletar o chat ───────────────────────
            salvar(conn, [registro], empresa)
            tickets_pagina.append(dados["ticket"])

            print(f"  [{idx}] {dados['ticket']} | {dados['contato']} | {dados['atendente']} | {dados['fila']} | última: {msg_hora} ({msg_de})")

        except Exception as e:
            print(f"  [{idx}] ERRO: {e}")

    return tickets_pagina


# ── paginação ─────────────────────────────────────────────────────────────────

def pagina_atual(page):
    """Retorna o número da página atual (1-based)."""
    try:
        btn = page.query_selector('[aria-current="true"][aria-label]')
        return int(btn.get_attribute("aria-label")) if btn else 1
    except Exception:
        return 1


def tem_proxima_pagina(page):
    """Retorna True se o botão próxima existe e não está desabilitado — um round-trip."""
    return page.evaluate("""
        () => {
            const btn = document.querySelector('[aria-label="Go to next page"]');
            return btn ? !btn.classList.contains('Mui-disabled') : false;
        }
    """)


def ir_para_proxima(page, num_pag):
    """Clica em 'próxima página' e aguarda o grid ter o conteúdo novo pronto."""
    page.click('[aria-label="Go to next page"]')
    nova_pag = num_pag + 1

    # Um único wait_for_function verifica simultaneamente:
    #   1. O indicador de página já mostra o número novo
    #   2. A primeira célula do grid já tem texto (não é a página antiga)
    # Retorna assim que ambos forem verdadeiros — sem sleep fixo.
    try:
        page.wait_for_function(
            f"""() => {{
                const ind = document.querySelector('[aria-current="true"]');
                if (!ind || parseInt(ind.getAttribute('aria-label')) !== {nova_pag}) return false;
                const cell = document.querySelector(
                    '[role="row"][aria-rowindex="2"] [data-field="ticket"] .MuiDataGrid-cellContent'
                );
                return cell && cell.innerText.trim().length > 0;
            }}""",
            timeout=1,
        )
    except PWTimeout:
        time.sleep(0.5)  # fallback caso o seletor não bata


# ── seletor de empresa ───────────────────────────────────────────────────────

def _empresa_input(page):
    """
    Retorna o locator correto do input de empresa.
    O campo tem label='Selecione uma opção' (diferente do campo Visualização
    cujo label é 'Visualização'). Usamos get_by_label para evitar pegar o errado.
    """
    return page.get_by_label("Selecione uma opção")


def _empresa_atual(page):
    """Lê o valor atual do input de empresa via atributo value."""
    try:
        return _empresa_input(page).get_attribute("value") or ""
    except Exception:
        return ""


def listar_empresas(page):
    """Abre o dropdown de empresa e retorna todas as opções do listbox."""
    inp = _empresa_input(page)
    inp.click()
    try:
        page.wait_for_selector('ul[role="listbox"] li[role="option"]', timeout=4000)
    except PWTimeout:
        page.keyboard.press("Escape")
        return []
    opcoes = page.evaluate("""
        () => Array.from(document.querySelectorAll('ul[role="listbox"] li[role="option"]'))
              .map(o => o.innerText.trim()).filter(t => t.length > 0)
    """)
    page.keyboard.press("Escape")
    time.sleep(0.3)
    return opcoes


def selecionar_empresa(page, nome_empresa):
    """Seleciona uma empresa no dropdown e aguarda o grid recarregar."""
    inp = _empresa_input(page)
    inp.click()
    page.wait_for_selector('ul[role="listbox"] li[role="option"]', timeout=4000)
    page.locator('ul[role="listbox"] li[role="option"]', has_text=nome_empresa).first.click()
    # Aguarda o grid esvaziar (WIT recarrega antes de preencher com dados novos)
    try:
        page.wait_for_function(
            """() => {
                const cell = document.querySelector(
                    '[role="row"][aria-rowindex="2"] [data-field="ticket"] .MuiDataGrid-cellContent'
                );
                return !cell || cell.innerText.trim().length === 0;
            }""",
            timeout=2000,
        )
    except PWTimeout:
        pass
    time.sleep(0.3)


def ir_para_pagina_1(page):
    """Volta para a página 1 do grid (workaround do bug do WIT ao trocar empresa)."""
    try:
        btn = page.query_selector('[aria-label="Go to first page"]')
        if btn:
            btn.click()
            time.sleep(0.3)
    except Exception:
        pass
    # aguarda grid ter conteúdo na linha 2
    try:
        page.wait_for_function(
            """() => {
                const cell = document.querySelector(
                    '[role="row"][aria-rowindex="2"] [data-field="ticket"] .MuiDataGrid-cellContent'
                );
                return cell && cell.innerText.trim().length > 0;
            }""",
            timeout=8000,
        )
    except PWTimeout:
        pass


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    conn = conectar_pg()
    todos_tickets = []
    total_registros = 0
    inicio_geral = time.time()

    progresso = carregar_progresso()
    empresa_inicio = progresso.get("empresa_idx", 0)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)

        print("Fazendo login...")
        page = login_e_navegar(browser)

        # Aguarda o grid carregar
        try:
            page.wait_for_function(
                """() => {
                    const cell = document.querySelector(
                        '[role="row"][aria-rowindex="2"] [data-field="ticket"] .MuiDataGrid-cellContent'
                    );
                    return cell && cell.innerText.trim().length > 0;
                }""",
                timeout=15000,
            )
        except PWTimeout:
            pass

        # Lê a lista de empresas uma única vez
        empresas = listar_empresas(page)
        if not empresas:
            print("ERRO: nenhuma empresa encontrada no dropdown.")
            browser.close()
            conn.close()
            return

        print(f"Empresas encontradas: {empresas}")
        if empresa_inicio > 0:
            print(f"Retomando a partir da empresa #{empresa_inicio}: {empresas[empresa_inicio]}\n")

        for emp_idx in range(empresa_inicio, len(empresas)):
            nome_raw = empresas[emp_idx]
            empresa = normalizar_empresa(nome_raw)
            print(f"\n{'='*50}")
            print(f"Empresa {emp_idx + 1}/{len(empresas)}: {nome_raw} → '{empresa}'")
            print(f"{'='*50}")

            salvar_progresso(emp_idx)

            # Seleciona a empresa e volta para página 1
            selecionar_empresa(page, nome_raw)
            confirmado = _empresa_atual(page)
            print(f"  Selecionado: '{confirmado}' → salvando como '{empresa}'")
            ir_para_pagina_1(page)

            num_pag = 1
            while True:
                inicio_pag = time.time()
                print(f"\n── Página {num_pag} [{empresa}] ──────────────────────────")

                tickets_pag = coletar_pagina(page, conn, empresa)
                todos_tickets += tickets_pag

                duracao = time.time() - inicio_pag
                total_registros += len(tickets_pag)
                print(f"  Tempo da página {num_pag}: {duracao:.1f}s | {len(tickets_pag)} registros")

                if not tem_proxima_pagina(page):
                    print(f"  Última página de '{empresa}' alcançada.")
                    break

                print("  Indo para próxima página...")
                ir_para_proxima(page, num_pag)
                num_pag += 1

        browser.close()

    # Tickets que sumiram do dashboard → Finalizado
    finalizar_ausentes(conn, todos_tickets)
    limpar_progresso()

    conn.close()
    duracao_total = time.time() - inicio_geral
    print(f"\nConcluído! {len(empresas)} empresa(s) | {total_registros} registros | {duracao_total:.1f}s total")


if __name__ == "__main__":
    main()
