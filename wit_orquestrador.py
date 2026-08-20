"""
Orquestrador: roda o pipeline de todas as empresas em ciclo contínuo.
Quando termina a última empresa do ciclo, começa o ciclo seguinte na hora —
roda pra sempre até ser interrompido (Ctrl+C).

Cada empresa roda isolada, uma de cada vez (evita sobrecarregar a máquina
com muitos navegadores simultâneos). Se uma empresa falhar (timeout de
login, lentidão do site, etc.), ela ganha 1 retentativa automática antes
de ser marcada como falha — e mesmo assim o orquestrador segue pra próxima
empresa sem travar o ciclo inteiro.

Uso:
    python wit_orquestrador.py                  -> 3 workers por empresa, headless
    python wit_orquestrador.py --num-workers 5  -> 5 workers por empresa
    python wit_orquestrador.py --no-headless    -> abre a janela do navegador
"""
import argparse
import datetime
import os
import subprocess
import sys
import time

if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

EMPRESAS = [
    ("ancore", "wit_ancore.py"),
    ("speed", "wit_speed.py"),
    ("valle", "wit_valle.py"),
    ("protege_car", "wit_protege_car.py"),
    ("br_truck", "wit_br_truck.py"),
]

TIMEOUT_POR_EMPRESA = 1200  # 20 min de seguranca — nenhum ciclo real chegou perto disso
MAX_TENTATIVAS = 2  # falhas transitorias (timeout de login, lentidao do site) ganham 1 retentativa


def _agora():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rodar_uma_vez(pasta, script, num_workers, headless):
    caminho = os.path.join(BASE_DIR, pasta, script)
    cmd = [sys.executable, caminho, "--num-workers", str(num_workers)]
    cmd += ["--headless"] if headless else ["--no-headless"]

    try:
        resultado = subprocess.run(cmd, cwd=os.path.join(BASE_DIR, pasta), timeout=TIMEOUT_POR_EMPRESA)
        return resultado.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[{pasta}] ERRO: excedeu o tempo limite ({TIMEOUT_POR_EMPRESA}s), processo abortado.")
        return False
    except Exception as e:
        print(f"[{pasta}] ERRO ao executar: {e}")
        return False


def rodar_empresa(pasta, script, num_workers, headless):
    print(f"\n{'=' * 60}")
    print(f"[{_agora()}] Iniciando {pasta}...")
    print(f"{'=' * 60}")

    inicio = time.time()
    ok = False
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        if tentativa > 1:
            print(f"[{pasta}] Tentativa {tentativa}/{MAX_TENTATIVAS} (falha transitória na anterior)...")
        ok = _rodar_uma_vez(pasta, script, num_workers, headless)
        if ok:
            break

    dt = time.time() - inicio
    status = "OK" if ok else "FALHOU"
    print(f"[{pasta}] {status} em {dt:.1f}s")
    return ok


def main():
    parser = argparse.ArgumentParser(description="Orquestrador: roda todas as empresas em loop contínuo")
    parser.add_argument("--num-workers", type=int, default=3, help="Workers por empresa (padrão: 3)")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True,
                         help="Roda sem abrir janela do navegador (padrão)")
    parser.add_argument("--no-headless", dest="headless", action="store_false",
                         help="Abre a janela do navegador (útil pra debugar)")
    args = parser.parse_args()

    ciclo = 0
    print(f"Orquestrador iniciado em {_agora()}.")
    print(f"{len(EMPRESAS)} empresa(s) por ciclo, {args.num_workers} worker(s) cada, headless={args.headless}.")
    print("Ctrl+C para parar.\n")

    try:
        while True:
            ciclo += 1
            inicio_ciclo = time.time()
            print(f"\n{'#' * 60}")
            print(f"# CICLO {ciclo} — início {_agora()}")
            print(f"{'#' * 60}")

            resultados = {}
            for pasta, script in EMPRESAS:
                resultados[pasta] = rodar_empresa(pasta, script, args.num_workers, args.headless)

            dt_ciclo = time.time() - inicio_ciclo
            falharam = [p for p, ok in resultados.items() if not ok]

            print(f"\n{'#' * 60}")
            print(f"# CICLO {ciclo} concluído em {dt_ciclo:.1f}s — fim {_agora()}")
            if falharam:
                print(f"# Empresa(s) com falha neste ciclo: {', '.join(falharam)}")
            else:
                print(f"# Todas as empresas concluídas sem erro.")
            print(f"{'#' * 60}")

    except KeyboardInterrupt:
        print(f"\n[{_agora()}] Orquestrador interrompido pelo usuário (Ctrl+C) após {ciclo} ciclo(s).")


if __name__ == "__main__":
    main()
