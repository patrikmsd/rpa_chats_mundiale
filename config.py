"""
Carrega credenciais de variáveis de ambiente / arquivo .env, em vez de
deixá-las gravadas no código-fonte. O .env não é versionado no Git
(está no .gitignore) — copie .env.example para .env e preencha os valores.
"""
import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _carregar_env(caminho):
    valores = {}
    if not os.path.exists(caminho):
        return valores
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, _, valor = linha.partition("=")
            valores[chave.strip()] = valor.strip().strip('"').strip("'")
    return valores


_env = _carregar_env(_ENV_PATH)


def _obter(chave):
    valor = os.environ.get(chave) or _env.get(chave)
    if not valor:
        raise RuntimeError(
            f"Variável '{chave}' não encontrada. Copie .env.example para .env "
            f"e preencha os valores, ou defina a variável de ambiente diretamente."
        )
    return valor


WITHUB_USERNAME = _obter("WITHUB_USERNAME")
WITHUB_PASSWORD = _obter("WITHUB_PASSWORD")

PG = dict(
    host=_obter("PG_HOST"),
    dbname=_obter("PG_DBNAME"),
    user=_obter("PG_USER"),
    password=_obter("PG_PASSWORD"),
)
