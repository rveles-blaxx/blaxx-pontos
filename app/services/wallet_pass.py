"""Apple Wallet .pkpass — Sprint 7 (S7-Wallet).

Refatoração de app/services/passkit.py com layout BlaXx oficial:
  * verde neon #59FD27 (label/foreground)
  * preto #0A0B0E (background)
  * logo BlaXx
  * primaryFields: balance
  * secondaryFields: tier (NÍVEL) + member name

API: build_blaxx_pkpass(user, state) -> bytes
     PassNotConfigured (re-export) — caller checa Config.apple_pass_configured()

Mantém compat com /card/pass que já chama passkit.build_pkpass — esse módulo é
o caminho novo. Em prod plug-and-play: setar APPLE_PASS_TYPE_ID + TEAM_ID +
APPLE_PASS_CERT_PATH (.p12) + APPLE_WWDR_CERT_PATH no env.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile

from ..config import Config

logger = logging.getLogger(__name__)


# Re-export pra caller não precisar saber sobre passkit antigo
class PassNotConfigured(RuntimeError):
    """Faltam certs Apple — endpoint /card/pass responde 503 com mensagem clara."""


# Cor oficial brand (vide MEMORY.md):
BLAXX_NEON_GREEN = "#59FD27"
BLAXX_BLACK = "#0A0B0E"


def _hex_to_rgb_str(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgb({r}, {g}, {b})"


def _solid_png(size: int, hex_bg: str) -> bytes:
    """Placeholder PNG sólido — substituído por assets oficiais se existirem."""
    try:
        from PIL import Image  # type: ignore

        h = hex_bg.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        img = Image.new("RGBA", (size, size), rgb + (255,))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        # Fallback PNG 1x1
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )


def _load_assets() -> dict[str, bytes]:
    """Carrega ícones/logo de app/assets/wallet/ se existirem; senão placeholders."""
    import os
    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "wallet")
    wanted = {
        "icon.png": 29, "icon@2x.png": 58, "icon@3x.png": 87,
        "logo.png": 160, "logo@2x.png": 320, "logo@3x.png": 480,
    }
    assets: dict[str, bytes] = {}
    for fname, size in wanted.items():
        path = os.path.join(base, fname)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                assets[fname] = fh.read()
        else:
            assets[fname] = _solid_png(size, BLAXX_BLACK)
    return assets


def build_blaxx_pass_dict(user, state: dict) -> dict:
    """Monta pass.json (storeCard) com layout BlaXx oficial."""
    masked_id = user.id[:8].upper()
    tier = state.get("tier", {}) or {}
    tier_label = tier.get("label", "Member")
    balance = state.get("balance_pts", 0)
    lifetime = state.get("lifetime_points", balance)

    back_fields = [
        {"key": "member_id", "label": "ID do membro", "value": masked_id},
        {"key": "lifetime", "label": "Pontos acumulados (vitalício)",
         "value": f"{lifetime:,}".replace(",", ".")},
        {"key": "terms", "label": "Sobre",
         "value": "Cartão de fidelidade BlaXx Pontos. "
                  "Nível baseado em pontos acumulados (lifetime), nunca diminui ao resgatar. "
                  "Suporte: contato@blaxxpontos.com.br"},
    ]
    if state.get("next_tier"):
        back_fields.insert(1, {
            "key": "next",
            "label": f"Próximo nível: {state['next_tier']['label']}",
            "value": f"Faltam {state['points_to_next']:,}".replace(",", ".") + " pts",
        })

    return {
        "formatVersion": 1,
        "passTypeIdentifier": Config.APPLE_PASS_TYPE_ID,
        "teamIdentifier": Config.APPLE_TEAM_ID,
        "organizationName": Config.APPLE_PASS_ORG_NAME,
        "serialNumber": user.id,
        "description": "BlaXx Pontos — Cartão de fidelidade",
        "logoText": "BlaXx",
        # Marca: verde neon nos labels, preto no fundo
        "foregroundColor": _hex_to_rgb_str(BLAXX_NEON_GREEN),
        "backgroundColor": _hex_to_rgb_str(BLAXX_BLACK),
        "labelColor": _hex_to_rgb_str(BLAXX_NEON_GREEN),
        "storeCard": {
            "primaryFields": [
                {"key": "balance", "label": "PONTOS",
                 "value": balance,
                 "numberStyle": "PKNumberStyleDecimal"},
            ],
            "secondaryFields": [
                {"key": "tier", "label": "NÍVEL", "value": tier_label},
                {"key": "member", "label": "MEMBRO", "value": user.name},
            ],
            "auxiliaryFields": [
                {"key": "lifetime", "label": "ACUMULADO",
                 "value": f"{lifetime:,}".replace(",", ".")},
            ],
            "backFields": back_fields,
        },
        "barcodes": [{
            "format": "PKBarcodeFormatQR",
            "message": user.id,
            "messageEncoding": "iso-8859-1",
            "altText": masked_id,
        }],
    }


def _sign_manifest(manifest_bytes: bytes) -> bytes:
    """Assina manifest com cert Apple + WWDR (PKCS#7 DER detached)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12

    cert_path = Config.APPLE_PASS_CERT_PATH
    wwdr_path = Config.APPLE_WWDR_CERT_PATH
    password = (Config.APPLE_PASS_CERT_PASSWORD or "").encode() or None

    with open(cert_path, "rb") as fh:
        cert_blob = fh.read()
    if cert_path.lower().endswith((".p12", ".pfx")):
        key, cert, _ = pkcs12.load_key_and_certificates(cert_blob, password)
    else:
        key = serialization.load_pem_private_key(cert_blob, password)
        cert = x509.load_pem_x509_certificate(cert_blob)

    with open(wwdr_path, "rb") as fh:
        wwdr_blob = fh.read()
    try:
        wwdr = x509.load_pem_x509_certificate(wwdr_blob)
    except ValueError:
        wwdr = x509.load_der_x509_certificate(wwdr_blob)

    options = [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]
    return (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(manifest_bytes)
        .add_signer(cert, key, hashes.SHA256())
        .add_certificate(wwdr)
        .sign(serialization.Encoding.DER, options)
    )


def build_blaxx_pkpass(user, state: dict) -> bytes:
    """Gera .pkpass com identidade visual BlaXx. PassNotConfigured se faltar cert."""
    if not Config.apple_pass_configured():
        raise PassNotConfigured(
            "Apple Wallet ainda não configurado — defina APPLE_PASS_TYPE_ID, "
            "APPLE_TEAM_ID, APPLE_PASS_CERT_PATH e APPLE_WWDR_CERT_PATH."
        )
    files: dict[str, bytes] = {}
    files["pass.json"] = json.dumps(
        build_blaxx_pass_dict(user, state), ensure_ascii=False
    ).encode("utf-8")
    files.update(_load_assets())

    manifest = {name: hashlib.sha1(data).hexdigest() for name, data in files.items()}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    files["manifest.json"] = manifest_bytes
    files["signature"] = _sign_manifest(manifest_bytes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()
