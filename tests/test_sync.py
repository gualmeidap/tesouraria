"""Testes do robô e da compatibilidade de criptografia com o app (index.html)."""
import re
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sync"))
import sync  # noqa: E402

HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def decrypt(blob: bytes, passphrase: str) -> bytes:
    """Espelho do decryptEnc() do index.html: salt(16) + iv(12) + ciphertext+tag."""
    salt, iv, ct = blob[:16], blob[16:28], blob[28:]
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=200_000).derive(passphrase.encode())
    return AESGCM(key).decrypt(iv, ct, None)


def test_encrypt_roundtrip():
    dados = b"PK\x03\x04 planilha de teste " * 100
    blob = sync.encrypt(dados, "frase-secreta")
    assert blob[:2] != b"PK", "saída não pode ser a planilha em claro"
    assert len(blob) == 16 + 12 + len(dados) + 16  # salt + iv + ct + tag GCM
    assert decrypt(blob, "frase-secreta") == dados


def test_encrypt_wrong_key_fails():
    blob = sync.encrypt(b"segredo", "certa")
    with pytest.raises(Exception):
        decrypt(blob, "errada")


def test_encrypt_uses_random_salt_and_iv():
    a, b = sync.encrypt(b"x", "k"), sync.encrypt(b"x", "k")
    assert a[:28] != b[:28]


def test_app_uses_same_crypto_parameters():
    """Se alguém mudar os parâmetros no app, o robô precisa mudar junto (e vice-versa)."""
    fn = HTML[HTML.index("async function decryptEnc") : HTML.index("let AUTO_OK")]
    assert "b.slice(0,16)" in fn and "b.slice(16,28)" in fn and "b.slice(28)" in fn
    assert "iterations:200000" in fn and "hash:'SHA-256'" in fn
    assert "name:'AES-GCM',length:256" in fn


def test_app_fetches_dados_enc():
    assert re.search(r"fetch\('dados\.enc\?t='", HTML)


def test_report_url_points_to_installments_export():
    assert sync.REPORT.endswith("?sales_installments_detailed=1")
    assert "{gid}" in sync.REPORT


def test_env_missing_exits(monkeypatch):
    monkeypatch.delenv("KEEPER_EMAIL", raising=False)
    with pytest.raises(SystemExit):
        sync.env("KEEPER_EMAIL")


def _robo_com_login(monkeypatch, tmp_path, login):
    from unittest.mock import MagicMock
    monkeypatch.setattr(sync, "sync_playwright", lambda: MagicMock())
    monkeypatch.setattr(sync, "login", login)
    monkeypatch.setattr(sync, "download_report", lambda page: b"PK\x03\x04planilha")
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setenv("KEEPER_EMAIL", "a@b.c")
    monkeypatch.setenv("KEEPER_SENHA", "x")
    monkeypatch.setenv("DATA_KEY", "chave")
    monkeypatch.setenv("OUT", str(tmp_path / "dados.enc"))


def test_robo_tenta_de_novo_quando_o_site_demora(monkeypatch, tmp_path):
    """Falhas de 02-11/10: Timeout no goto/click. Agora ele repete e publica se alguma tentativa der certo."""
    chamadas = {"n": 0}

    def login_instavel(page, email, senha):
        chamadas["n"] += 1
        if chamadas["n"] < 3:
            raise RuntimeError("Page.goto: Timeout 60000ms exceeded.")

    _robo_com_login(monkeypatch, tmp_path, login_instavel)
    sync.main()
    assert chamadas["n"] == 3
    assert (tmp_path / "dados.enc").exists()


def test_robo_desiste_depois_das_tentativas(monkeypatch, tmp_path):
    def login_quebrado(page, email, senha):
        raise RuntimeError("sempre falha")

    _robo_com_login(monkeypatch, tmp_path, login_quebrado)
    with pytest.raises(SystemExit) as e:
        sync.main()
    assert f"{sync.TENTATIVAS} tentativas" in str(e.value)
    assert not (tmp_path / "dados.enc").exists(), "não pode gravar dados.enc se todas falharam"


def test_robo_nao_depende_de_networkidle_nem_de_chamadas_sem_limite():
    src = (ROOT / "sync" / "sync.py").read_text(encoding="utf-8")
    assert 'goto(SITE, wait_until="networkidle"' not in src
    assert src.count("AbortController") >= 2          # os dois fetch têm limite de tempo
    assert sync.NAV_TIMEOUT >= 60_000 and sync.TENTATIVAS >= 3


def test_rodar_ps1_tem_prazo_maximo_e_espera_a_rede():
    ps = (ROOT / "sync" / "rodar.ps1").read_text(encoding="utf-8-sig")
    assert "WaitForExit(" in ps and "taskkill" in ps    # em 12/10 travou 2 h sem limite
    assert "GetHostAddresses" in ps                      # espera a internet voltar depois de ligar/acordar


def test_dados_enc_is_ciphertext_if_present():
    enc = ROOT / "dados.enc"
    if not enc.exists():
        pytest.skip("dados.enc ainda não publicado")
    b = enc.read_bytes()
    assert len(b) > 28 and b[:2] != b"PK"
