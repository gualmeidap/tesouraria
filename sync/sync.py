"""
Robô de sincronização: entra no Keeper, baixa a planilha "Vendas Parcelas Detalhado"
(Fluxo de Recebimento) e grava `dados.enc` criptografado (AES-256-GCM) para o app ler.

Variáveis de ambiente (nunca coloque no código):
  KEEPER_EMAIL  - e-mail de login (ou CPF) da conta no Keeper
  KEEPER_SENHA  - senha da conta
  DATA_KEY      - senha que o app vai pedir para abrir os dados (defina uma frase forte)
  OUT           - (opcional) caminho de saída, padrão: dados.enc na pasta do repositório

Uso local (para testar):
  pip install -r sync/requirements.txt
  playwright install chromium
  set KEEPER_EMAIL=... & set KEEPER_SENHA=... & set DATA_KEY=...   (PowerShell: $env:KEEPER_EMAIL="...")
  python sync/sync.py
"""
import base64
import os
import re
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

SITE = "https://conta.keeperformaturas.com.br/"
REPORT = "https://reports.api.keeperformaturas.com.br/v1/report/sales/group/{gid}?sales_installments_detailed=1"
ROOT = Path(__file__).resolve().parent.parent

# O site da Keeper é um app Angular que às vezes demora (ou nem termina) de carregar em modo headless.
# Por isso: espera generosa, nenhuma chamada sem limite e várias tentativas com pausa crescente.
NAV_TIMEOUT = 60_000        # ms para a página abrir / o login aparecer
ACAO_TIMEOUT = 45_000       # ms para qualquer clique / espera
DOWNLOAD_TIMEOUT = 90_000   # ms para a API de relatórios responder
TENTATIVAS = 3
PAUSA_ENTRE_TENTATIVAS = 30  # segundos (multiplicado pelo nº da tentativa)


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Falta a variável de ambiente {name}")
    return v


def encrypt(data: bytes, passphrase: str) -> bytes:
    """Formato: salt(16) + iv(12) + ciphertext+tag. O app usa WebCrypto com os mesmos parâmetros."""
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000).derive(passphrase.encode())
    return salt + iv + AESGCM(key).encrypt(iv, data, None)


def dismiss_popups(page):
    """Fecha o aviso do app (Esc) e remove o fundo escuro que trava os cliques.
    Não aceita cookies nem LGPD em nome dela — só tira o que bloqueia a tela."""
    for _ in range(3):
        if page.locator("mat-dialog-container").count() == 0:
            break
        page.keyboard.press("Escape")
        time.sleep(0.6)
    page.evaluate("""() => {
        document.querySelectorAll('.cdk-overlay-backdrop').forEach(e => e.remove());
        document.querySelectorAll('.cdk-overlay-pane').forEach(e => { if (!e.querySelector('input')) e.remove(); });
    }""")
    time.sleep(0.3)


def open_login_form(page):
    """Vai da tela inicial até o formulário (campos de e-mail e senha visíveis)."""
    # "networkidle" nunca chega quando o site mantém conexões abertas (foi o que derrubou o robô em 03, 09 e 11/10);
    # basta o HTML carregar e depois esperar a tela de login aparecer de verdade.
    page.goto(SITE, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    senha = page.locator("input[type='password']")
    botao_login = page.get_by_text(re.compile(r"fazer login", re.I))
    senha.or_(botao_login).first.wait_for(state="visible", timeout=NAV_TIMEOUT)
    time.sleep(1.5)
    dismiss_popups(page)
    if senha.count() == 0 or not senha.first.is_visible():
        botao_login.first.click(timeout=ACAO_TIMEOUT)
        senha.first.wait_for(state="visible", timeout=ACAO_TIMEOUT)
    email = page.get_by_placeholder(re.compile(r"mail|cpf", re.I))
    if email.count() == 0:
        email = page.locator("input:not([type='password'])").filter(visible=True)
    return email.first, senha.first


def login(page, email: str, senha: str):
    campo_email, campo_senha = open_login_form(page)
    campo_email.focus()
    campo_email.press_sequentially(email, delay=20)
    campo_senha.focus()
    campo_senha.press_sequentially(senha, delay=20)
    time.sleep(0.5)

    # Diagnóstico: registra status/mensagem das respostas de autenticação (sem tokens)
    respostas = []
    def on_response(resp):
        if re.search(r"auth|login|session", resp.url, re.I) and "google" not in resp.url:
            try:
                corpo = resp.text()
                m = re.search(r'"(message|detail|error)"\s*:\s*"([^"]{0,120})"', corpo)
                msg = m.group(0) if m else ""
            except Exception:
                msg = ""
            respostas.append(f"{resp.status} {resp.url.split('.br')[-1]} {msg}")
    page.on("response", on_response)
    page.on("request", lambda r: respostas.append(f"REQ {r.method} {r.url.split('.br')[-1]}") if "api.keeper" in r.url and re.search(r"auth|login", r.url) else None)
    page.on("requestfailed", lambda r: respostas.append(f"FALHA {r.url.split('.br')[-1]} {r.failure}") if "api.keeper" in r.url else None)

    botao = page.locator("button[type='submit'], button", has_text=re.compile(r"entrar", re.I))
    if botao.count():
        botao.first.click()
    else:
        campo_senha.press("Enter")
    try:
        # Logado quando o painel aparece (menu lateral / "Painel inicial")
        page.get_by_text(re.compile(r"painel inicial", re.I)).first.wait_for(state="visible", timeout=45000)
    except PWTimeout:
        avisos = page.evaluate("""() => [...document.querySelectorAll('snack-bar-container, mat-snack-bar-container, simple-snack-bar, .mat-mdc-snack-bar-label, mat-error, .alert, .error, .toast')].map(e => e.innerText.trim()).filter(Boolean)""")
        raise RuntimeError(f"login não avançou. respostas={respostas} avisos={avisos} url={page.url}")
    try:  # pode nunca ficar "ocioso" (conexões abertas); o painel já apareceu, então não é motivo para falhar
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    dismiss_popups(page)


def download_report(page) -> bytes:
    gid = page.evaluate("localStorage.getItem('current_group')")
    if not gid:
        # Tenta descobrir pela chamada /auth/me (com limite de tempo: fetch sem limite pode travar para sempre)
        gid = page.evaluate("""async (ms) => {
            const ctrl = new AbortController(); const t = setTimeout(() => ctrl.abort(), ms);
            try {
                const r = await fetch('https://main.api.keeperformaturas.com.br/v1/auth/me', {credentials:'include', signal: ctrl.signal});
                const j = await r.json(); const c = (j.contributors||[])[0]; return c ? c.group_id : null;
            } finally { clearTimeout(t); } }""", ACAO_TIMEOUT)
    if not gid:
        raise RuntimeError("Não encontrei a turma (group_id) após o login.")
    b64 = page.evaluate("""async ([url, ms]) => {
        const ctrl = new AbortController(); const t = setTimeout(() => ctrl.abort(), ms);
        try {
            const r = await fetch(url, {credentials:'include', signal: ctrl.signal});
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const buf = new Uint8Array(await r.arrayBuffer());
            let s = ''; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
            return btoa(s);
        } finally { clearTimeout(t); } }""", [REPORT.format(gid=gid), DOWNLOAD_TIMEOUT])
    data = base64.b64decode(b64)
    if data[:2] != b"PK":
        raise RuntimeError("A resposta não parece ser um .xlsx (o login pode ter falhado).")
    return data


def main():
    email, senha, data_key = env("KEEPER_EMAIL"), env("KEEPER_SENHA"), env("DATA_KEY")
    out = Path(os.environ.get("OUT") or ROOT / "dados.enc")
    xlsx, ultimo_erro = None, ""
    with sync_playwright() as p:
        for tentativa in range(1, TENTATIVAS + 1):
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(locale="pt-BR", viewport={"width": 1280, "height": 900})
            ctx.set_default_timeout(ACAO_TIMEOUT)
            ctx.set_default_navigation_timeout(NAV_TIMEOUT)
            page = ctx.new_page()
            try:
                login(page, email, senha)
                xlsx = download_report(page)
                break
            except Exception as e:  # deixa uma captura de tela para diagnosticar
                ultimo_erro = f"{type(e).__name__}: {e}".split("\n")[0][:200]
                print(f"Tentativa {tentativa}/{TENTATIVAS} falhou: {ultimo_erro}", flush=True)
                try:
                    page.screenshot(path=str(ROOT / "sync-erro.png"), full_page=True)
                except Exception:
                    pass
            finally:
                browser.close()
            if tentativa < TENTATIVAS:
                time.sleep(PAUSA_ENTRE_TENTATIVAS * tentativa)
    if xlsx is None:
        raise SystemExit(f"Falhou após {TENTATIVAS} tentativas: {ultimo_erro} (veja sync-erro.png)")
    out.write_bytes(encrypt(xlsx, data_key))
    print(f"OK: {len(xlsx):,} bytes baixados -> {out} ({out.stat().st_size:,} bytes criptografados)")


if __name__ == "__main__":
    main()
