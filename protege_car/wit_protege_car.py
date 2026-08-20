"""
Pipeline completo da Protege Car num arquivo só: login -> N workers em paralelo
-> finalizacao. Um unico comando roda tudo.

Uso:
    python wit_protege_car.py                      -> 3 workers, headless
    python wit_protege_car.py --num-workers 5      -> 5 workers, headless
    python wit_protege_car.py --no-headless        -> abre a janela do navegador

Por baixo dos panos, o proprio arquivo se relança como subprocesso pra cada
worker (com --worker-id), porque o Playwright sync API nao aceita ser
comandado por threads diferentes dentro do mesmo processo — so processos
separados de verdade funcionam em paralelo.

Cada worker grava sua fatia de tickets em wit_protege_car_tickets_worker{id}.json;
depois que todos terminam, o processo principal une as listas e marca como
'Finalizado' os tickets da Protege Car que sumiram da fila "Em Atendimento"
(escopado por empresa='protege car' AND chat='mundiale' — nunca mexe em outras
empresas).
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

import psycopg2
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

# wit.py mora na pasta pai (raiz do projeto) — precisa entrar no sys.path
# pra este script funcionar de dentro da subpasta da empresa.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wit import (
    coletar_pagina,
    conectar_pg,
    ir_para_pagina_1,
    ir_para_proxima,
    listar_empresas,
    login_e_navegar,
    normalizar_empresa,
    selecionar_empresa,
    tem_proxima_pagina,
)
from config import PG, WITHUB_USERNAME, WITHUB_PASSWORD

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

EMPRESA_ALVO = "Protege Car Atendimento"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_STATE_FILE = os.path.join(BASE_DIR, "wit_protege_car_storage_state.json")
DASHBOARD_URL = "https://app.withub.ai/dashboard"

TABELA = "public.historico_atendimentos"


# ── helpers de espera ──────────────────────────────────────────────────────

def esperar_grid(page):
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


# ── etapa 1: login + salvar sessao ────────────────────────────────────────

def fazer_login_e_salvar_sessao():
    print("[login] Fazendo login e salvando sessão...")
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()
        page.goto("https://account.withub.ai/login")
        page.fill("#username", WITHUB_USERNAME)
        page.fill("#password", WITHUB_PASSWORD)
        page.click("button[name='login']")
        page.get_by_role("button", name="Acessar").first.click()
        page.wait_for_timeout(5000)

        pages = context.pages
        target = pages[-1] if len(pages) > 1 else page
        target.locator("span", has_text="speed").first.click()
        target.get_by_role("tab", name="Em Atendimento").click()
        target.wait_for_timeout(2000)
        esperar_grid(target)

        context.storage_state(path=STORAGE_STATE_FILE)
        browser.close()
    print(f"[login] Sessão salva em {time.time() - t0:.1f}s")


# ── etapa 2: worker (roda como subprocesso de si mesmo) ──────────────────

def abrir_com_sessao_salva(browser, tag):
    if not os.path.exists(STORAGE_STATE_FILE):
        return None
    try:
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            storage_state=STORAGE_STATE_FILE,
        )
        page = context.new_page()
        page.goto(DASHBOARD_URL)
        page.wait_for_load_state("domcontentloaded")

        if "login" in page.url:
            print(f"{tag} Sessão salva expirada, caindo pro login completo.")
            context.close()
            return None

        page.locator("span", has_text="speed").first.click()
        page.get_by_role("tab", name="Em Atendimento").click()
        page.wait_for_timeout(2000)
        return page
    except Exception as e:
        print(f"{tag} Falha ao reaproveitar sessão ({e}), caindo pro login completo.")
        return None


def rodar_worker(worker_id, num_workers, headless):
    tag = f"[worker {worker_id}/{num_workers}]"
    conn = conectar_pg()
    total_registros = 0
    paginas_processadas = 0
    todos_tickets = []
    inicio = time.time()

    with sync_playwright() as p:
        launch_kwargs = dict(headless=headless)
        if headless:
            launch_kwargs["args"] = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        browser = p.chromium.launch(**launch_kwargs)

        print(f"{tag} Tentando reaproveitar sessão salva...")
        page = abrir_com_sessao_salva(browser, tag)
        if page is None:
            print(f"{tag} Fazendo login completo...")
            page = login_e_navegar(browser)
        else:
            print(f"{tag} Sessão reaproveitada, sem login.")
        esperar_grid(page)

        empresas = listar_empresas(page)
        if EMPRESA_ALVO not in empresas:
            print(f"{tag} ERRO: '{EMPRESA_ALVO}' não encontrada no dropdown ({empresas}).")
            browser.close()
            conn.close()
            _salvar_tickets_worker(worker_id, [])
            return

        empresa = normalizar_empresa(EMPRESA_ALVO)
        selecionar_empresa(page, EMPRESA_ALVO)
        ir_para_pagina_1(page)

        num_pag = 1
        while True:
            minha_pagina = (num_pag - 1) % num_workers == worker_id

            if minha_pagina:
                t0 = time.time()
                tickets_pag = coletar_pagina(page, conn, empresa)
                total_registros += len(tickets_pag)
                todos_tickets += tickets_pag
                paginas_processadas += 1
                print(f"{tag} Página {num_pag}: {time.time() - t0:.1f}s | {len(tickets_pag)} registros")
            else:
                print(f"{tag} Página {num_pag}: de outro worker, pulando.")

            if not tem_proxima_pagina(page):
                print(f"{tag} Última página alcançada (página {num_pag}).")
                break

            ir_para_proxima(page, num_pag)
            num_pag += 1

        browser.close()

    conn.close()
    _salvar_tickets_worker(worker_id, todos_tickets)

    dt = time.time() - inicio
    print(f"{tag} Concluído! {paginas_processadas} página(s) próprias | {total_registros} registros | {dt:.1f}s total")


def _tickets_file(worker_id):
    return os.path.join(BASE_DIR, f"wit_protege_car_tickets_worker{worker_id}.json")


def _salvar_tickets_worker(worker_id, tickets):
    with open(_tickets_file(worker_id), "w") as f:
        json.dump(tickets, f)


# ── etapa 3: finalizacao ──────────────────────────────────────────────────

def finalizar(num_workers):
    arquivos = [_tickets_file(i) for i in range(num_workers) if os.path.exists(_tickets_file(i))]

    if not arquivos:
        print("[finalizar] Nenhum arquivo de tickets encontrado — pulando finalização.")
        return

    todos_tickets = []
    for arq in arquivos:
        with open(arq) as f:
            todos_tickets += json.load(f)

    tickets_ativos = sorted(set(int(t) for t in todos_tickets if str(t).isdigit()))
    print(f"[finalizar] Total de tickets ativos únicos: {len(tickets_ativos)}")

    if not tickets_ativos:
        print("[finalizar] ABORTANDO: lista de tickets ativos veio vazia — "
              "não vou marcar tudo da Protege Car como Finalizado por segurança.")
        return

    conn = psycopg2.connect(**PG)
    cur = conn.cursor()
    cur.execute(f"""
        UPDATE {TABELA}
        SET status = 'Finalizado', em_espera = FALSE
        WHERE status = 'Em atendimento'
          AND empresa = %s
          AND chat = 'mundiale'
          AND protocolo_id NOT IN %s
    """, ('protege car', tuple(tickets_ativos)))
    finalizados = cur.rowcount
    conn.commit()
    conn.close()
    print(f"[finalizar] {finalizados} ticket(s) da Protege Car marcado(s) como 'Finalizado'.")

    for arq in arquivos:
        os.remove(arq)
    print("[finalizar] Arquivos de progresso dos workers removidos.")


# ── orquestrador ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pipeline completo da Protege Car: login -> workers paralelos -> finalização")
    parser.add_argument("--worker-id", type=int, default=None,
                         help=argparse.SUPPRESS)  # uso interno: o proprio script se relança com isso
    parser.add_argument("--num-workers", type=int, default=3, help="Quantos workers rodar em paralelo (padrão: 3)")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True,
                         help="Roda sem abrir janela do navegador (padrão)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                         help="Abre a janela do navegador (útil pra debugar)")
    args = parser.parse_args()

    # Modo worker: este processo foi relançado pelo orquestrador pra fazer 1 fatia do trabalho.
    if args.worker_id is not None:
        rodar_worker(args.worker_id, args.num_workers, args.headless)
        return

    # Modo orquestrador: roda o pipeline completo.
    if not (1 <= args.num_workers):
        raise SystemExit("--num-workers precisa ser >= 1")

    inicio_geral = time.time()

    fazer_login_e_salvar_sessao()

    print(f"\n[main] Disparando {args.num_workers} worker(s) em paralelo...")
    cmd_base = [sys.executable, os.path.abspath(__file__), "--num-workers", str(args.num_workers)]
    cmd_base += ["--headless"] if args.headless else ["--no-headless"]

    processos = []
    for wid in range(args.num_workers):
        cmd = cmd_base + ["--worker-id", str(wid)]
        proc = subprocess.Popen(cmd, cwd=BASE_DIR)
        processos.append(proc)

    codigos = [proc.wait() for proc in processos]
    if any(c != 0 for c in codigos):
        print(f"\n[main] AVISO: worker(s) terminaram com erro (códigos: {codigos}).")

    finalizar(args.num_workers)

    print(f"\n[main] Pipeline completo em {time.time() - inicio_geral:.1f}s total")


if __name__ == "__main__":
    main()
