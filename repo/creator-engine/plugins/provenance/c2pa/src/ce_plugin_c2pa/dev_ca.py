"""A throwaway C2PA test CA for dev and test (§27): a P-256 root and an ES256 leaf with the key
usage C2PA validators accept (digitalSignature; EKU emailProtection + documentSigning; not a CA,
SKI/AKI present). Generated on first use into a scratch directory; never committed, never trusted."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

__all__ = ["DevCertificates", "ensure_dev_ca"]

DOCUMENT_SIGNING = x509.ObjectIdentifier("1.3.6.1.5.5.7.3.36")


@dataclass(frozen=True)
class DevCertificates:
    chain_pem: Path  # leaf + root
    key_pem: Path
    root_pem: Path


def _name(common: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "DE"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Creator Engine DEV (untrusted)"),
            x509.NameAttribute(NameOID.COMMON_NAME, common),
        ]
    )


def ensure_dev_ca(directory: Path, *, validity_days: int = 30) -> DevCertificates:
    directory.mkdir(parents=True, exist_ok=True)
    out = DevCertificates(directory / "chain.pem", directory / "leaf.key", directory / "root.pem")
    if out.chain_pem.is_file() and out.key_pem.is_file() and out.root_pem.is_file():
        leaf = x509.load_pem_x509_certificates(out.chain_pem.read_bytes())[0]
        if leaf.not_valid_after_utc > dt.datetime.now(dt.UTC) + dt.timedelta(days=1):
            return out
    now = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_name = _name("Creator Engine DEV Root CA")
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=validity_days + 1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name("Creator Engine DEV signer"))
        .issuer_name(root_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION, DOCUMENT_SIGNING]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    out.root_pem.write_bytes(root.public_bytes(pem))
    out.chain_pem.write_bytes(leaf.public_bytes(pem) + root.public_bytes(pem))
    out.key_pem.write_bytes(
        leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    out.key_pem.chmod(0o600)
    return out
