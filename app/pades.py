"""Sello digital de los informes: PAdES dentro del PDF y CMS/PKCS#7 separado (.p7s) para el Excel.

Por defecto el gestor genera su propio certificado (autofirmado) en data/certs. Para produccion se
recomienda un certificado corporativo: config.yaml -> report.cert_pfx (+ contrasena en la variable de
entorno indicada en report.cert_password_env).
"""
from __future__ import annotations

import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from .config import BASE_DIR, get_settings

_lock = threading.Lock()


def _own_cert_paths() -> tuple[Path, Path]:
    d = get_settings().data_path / "certs"
    return d / "gestor_sello.pfx", d / "gestor_sello.clave"


def _generate_own_cert(pfx: Path, pw_file: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    org = get_settings().report.organization
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gestor de Registros WinCC - sello de informes"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, org)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    pw = secrets.token_urlsafe(24)
    data = pkcs12.serialize_key_and_certificates(b"gestor-sello", key, cert, None,
                                                 serialization.BestAvailableEncryption(pw.encode()))
    pfx.parent.mkdir(parents=True, exist_ok=True)
    pw_file.write_text(pw, encoding="utf-8")
    pfx.write_bytes(data)


def cert_source() -> tuple[Path, bytes | None]:
    cfg = get_settings().report
    if cfg.cert_pfx:
        p = Path(cfg.cert_pfx)
        p = p if p.is_absolute() else (BASE_DIR / p).resolve()
        pw = os.environ.get(cfg.cert_password_env)
        return p, pw.encode() if pw else None
    pfx, pw_file = _own_cert_paths()
    with _lock:
        if not pfx.exists():
            _generate_own_cert(pfx, pw_file)
    return pfx, pw_file.read_text(encoding="utf-8").strip().encode()


def certificate_pem() -> bytes:
    """Certificado publico (para instalarlo como confiable en el lector de PDF o validar)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    pfx, pw = cert_source()
    _, cert, _ = pkcs12.load_key_and_certificates(pfx.read_bytes(), pw)
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_and_cert():
    from cryptography.hazmat.primitives.serialization import pkcs12
    pfx, pw = cert_source()
    key, cert, _ = pkcs12.load_key_and_certificates(pfx.read_bytes(), pw)
    if key is None or cert is None:
        raise RuntimeError(f"No se pudo cargar el certificado {pfx}")
    return key, cert


def sign_detached(data: bytes) -> bytes:
    """Sello digital de un fichero cualquiera (p. ej. el .xlsx): firma CMS/PKCS#7 separada, DER (.p7s).

    Se verifica con verify_detached() o con:
      openssl cms -verify -binary -inform DER -in f.p7s -content f.xlsx -CAfile gestor_sello.pem -purpose any"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs7

    key, cert = _key_and_cert()
    return (pkcs7.PKCS7SignatureBuilder().set_data(data).add_signer(cert, key, hashes.SHA256())
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature, pkcs7.PKCS7Options.Binary]))


def verify_detached(data: bytes, p7s: bytes) -> dict:
    """Comprueba un sello .p7s sobre 'data'. 'trusted' = lo firmo el certificado actual del gestor."""
    import hashlib

    from asn1crypto import cms
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    out = {"valid": False, "digest_ok": False, "signature_ok": False, "trusted": False, "signer": None,
           "signed_at": None}
    try:
        sd = cms.ContentInfo.load(p7s)["content"]
        si = sd["signer_infos"][0]
        sid = si["sid"].chosen
        cert_asn = next(c.chosen for c in sd["certificates"]
                        if c.chosen.serial_number == sid["serial_number"].native and c.chosen.issuer == sid["issuer"])
        attrs = si["signed_attrs"]
        for a in attrs:
            if a["type"].native == "message_digest":
                out["digest_ok"] = a["values"][0].native == hashlib.sha256(data).digest()
            elif a["type"].native == "signing_time":
                out["signed_at"] = a["values"][0].native
        signed = b"\x31" + attrs.dump()[1:]            # [0] IMPLICIT -> SET OF, como se firmo
        cert = x509.load_der_x509_certificate(cert_asn.dump())
        out["signer"] = cert_asn.subject.native.get("common_name")
        pub = cert.public_key()
        sig = si["signature"].native
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        else:  # pragma: no cover - tipos de clave no usados
            raise InvalidSignature("tipo de clave no soportado")
        out["signature_ok"] = True
        _, own = _key_and_cert()
        out["trusted"] = cert.public_bytes(serialization.Encoding.DER) == own.public_bytes(serialization.Encoding.DER)
    except (InvalidSignature, ValueError, KeyError, StopIteration, TypeError):
        pass
    out["valid"] = out["digest_ok"] and out["signature_ok"]
    return out


def sign_pdf(pdf: bytes, reason: str) -> bytes:
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers

    pfx, pw = cert_source()
    signer = signers.SimpleSigner.load_pkcs12(str(pfx), passphrase=pw)
    if signer is None:
        raise RuntimeError(f"No se pudo cargar el certificado {pfx}")
    w = IncrementalPdfFileWriter(BytesIO(pdf))
    meta = signers.PdfSignatureMetadata(field_name="SelloGestor", reason=reason[:200],
                                        location=get_settings().report.location, md_algorithm="sha256",
                                        subfilter=fields.SigSeedSubFilter.PADES)
    out = signers.sign_pdf(w, meta, signer=signer)
    return out.getvalue()
