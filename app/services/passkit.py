"""Geração do cartão Blaxx como Apple Wallet pass (.pkpass).

Arquitetura (pronto-para-plugar):
  * O .pkpass é um ZIP contendo pass.json + imagens + manifest.json + signature.
  * A `signature` é uma assinatura PKCS#7 (detached) do manifest.json, feita
    com o certificado Pass Type ID (Apple Developer) + chave privada, incluindo
    o certificado intermediário WWDR da Apple.
  * Enquanto os certificados NÃO estiverem configurados (ver
    Config.apple_pass_configured()), build_pkpass() levanta PassNotConfigured —
    o endpoint /card/pass responde 503 com instrução, e os frontends mostram
    "em breve". Nenhuma credencial é tratada em código; tudo vem de env/arquivos.

Para ativar: ver instruções em config.py (seção Apple Wallet).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

from ..config import Config


class PassNotConfigured(RuntimeError):
    """Levantada quando faltam os certificados Apple para assinar o .pkpass."""


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _hex_to_rgb(hex_color: str) -> str:
    """'#0B0B0C' -> 'rgb(11, 11, 12)' (formato que o PassKit espera)."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgb({r}, {g}, {b})"


def _solid_png(size: int, hex_bg: str) -> bytes:
    """PNG quadrado de cor sólida (placeholder de ícone/logo).

    Usa Pillow se disponível; senão devolve um PNG 1x1 mínimo válido. Substitua
    por assets oficiais da marca colocando icon.png/logo.png em app/assets/wallet/.
    """
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
    except Exception:
        # PNG 1x1 transparente (fallback mínimo, ainda válido p/ o pacote)
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )


def _load_pass_assets() -> dict[str, bytes]:
    """Carrega ícones/logo oficiais de app/assets/wallet/ se existirem; senão
    gera placeholders sólidos. Retorna o set mínimo exigido pela Apple.
    """
    import os

    base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "wallet")
    wanted = {
        "icon.png": 29, "icon@2x.png": 58, "icon@3x.png": 87,
        "logo.png": 160, "logo@2x.png": 320,
    }
    assets: dict[str, bytes] = {}
    for fname, size in wanted.items():
        path = os.path.join(base, fname)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                assets[fname] = fh.read()
        else:
            assets[fname] = _solid_png(size, "#0B0B0C")
    return assets


# --------------------------------------------------------------------------- #
# pass.json                                                                   #
# --------------------------------------------------------------------------- #
def build_pass_dict(user, state: dict) -> dict:
    """Monta o dicionário pass.json (storeCard) a partir do estado de tier."""
    tier = state["tier"]
    masked_id = user.id[:8].upper()
    fields_back = [
        {"key": "member_id", "label": "ID do membro", "value": masked_id},
        {"key": "lifetime", "label": "Pontos acumulados (vitalício)",
         "value": f'{state["lifetime_points"]:,}'.replace(",", ".")},
        {"key": "terms", "label": "Sobre",
         "value": "Cartão de fidelidade BlaXx. O nível é definido pelos "
                  "pontos acumulados e nunca diminui ao resgatar."},
    ]
    if state.get("next_tier"):
        fields_back.insert(1, {
            "key": "next",
            "label": f'Próximo nível: {state["next_tier"]["label"]}',
            "value": f'Faltam {state["points_to_next"]:,}'.replace(",", ".") + " pts",
        })

    return {
        "formatVersion": 1,
        "passTypeIdentifier": Config.APPLE_PASS_TYPE_ID,
        "teamIdentifier": Config.APPLE_TEAM_ID,
        "organizationName": Config.APPLE_PASS_ORG_NAME,
        "serialNumber": user.id,
        "description": "Cartão Blaxx",
        "logoText": "Blaxx",
        "foregroundColor": _hex_to_rgb(tier.get("text_color", "#FFFFFF")),
        "backgroundColor": _hex_to_rgb(tier.get("color", "#0B0B0C")),
        "labelColor": _hex_to_rgb(tier.get("text_color", "#FFFFFF")),
        "storeCard": {
            "primaryFields": [
                {"key": "balance", "label": "PONTOS",
                 "value": state["balance_pts"]},
            ],
            "secondaryFields": [
                {"key": "tier", "label": "NÍVEL", "value": tier["label"]},
                {"key": "member", "label": "MEMBRO", "value": user.name},
            ],
            "auxiliaryFields": [
                {"key": "lifetime", "label": "ACUMULADO",
                 "value": f'{state["lifetime_points"]:,}'.replace(",", ".")},
            ],
            "backFields": fields_back,
        },
        "barcodes": [{
            "format": "PKBarcodeFormatQR",
            "message": user.id,
            "messageEncoding": "iso-8859-1",
            "altText": masked_id,
        }],
    }


# --------------------------------------------------------------------------- #
# Assinatura PKCS#7                                                            #
# --------------------------------------------------------------------------- #
def _sign_manifest(manifest_bytes: bytes) -> bytes:
    """Assina o manifest.json com o cert Pass Type ID + WWDR (PKCS#7 DER)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12

    cert_path = Config.APPLE_PASS_CERT_PATH
    wwdr_path = Config.APPLE_WWDR_CERT_PATH
    password = (Config.APPLE_PASS_CERT_PASSWORD or "").encode() or None

    with open(cert_path, "rb") as fh:
        cert_blob = fh.read()

    # Aceita PKCS#12 (.p12/.pfx) — cert + chave juntos — ou PEM (cert + key).
    if cert_path.lower().endswith((".p12", ".pfx")):
        key, cert, _add = pkcs12.load_key_and_certificates(cert_blob, password)
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


# --------------------------------------------------------------------------- #
# Pacote .pkpass                                                              #
# --------------------------------------------------------------------------- #
def build_pkpass(user, state: dict) -> bytes:
    """Gera o .pkpass assinado. Levanta PassNotConfigured se faltar certificado."""
    if not Config.apple_pass_configured():
        raise PassNotConfigured(
            "Apple Wallet ainda não configurado: faltam APPLE_PASS_TYPE_ID, "
            "APPLE_TEAM_ID, APPLE_PASS_CERT_PATH e/ou APPLE_WWDR_CERT_PATH."
        )

    files: dict[str, bytes] = {}
    files["pass.json"] = json.dumps(
        build_pass_dict(user, state), ensure_ascii=False
    ).encode("utf-8")
    files.update(_load_pass_assets())

    # manifest.json = SHA-1 de cada arquivo
    manifest = {name: hashlib.sha1(data).hexdigest() for name, data in files.items()}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    files["manifest.json"] = manifest_bytes

    # signature = PKCS#7 detached do manifest
    files["signature"] = _sign_manifest(manifest_bytes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()
