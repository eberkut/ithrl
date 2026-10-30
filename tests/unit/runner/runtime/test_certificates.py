"""Configured trust bundles enter disposable state without parent-directory access."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ithrl.runner.runtime.certificates import snapshot_certificate_files


def test_configured_bundles_are_private_copies_with_rewritten_paths(tmp_path: Path) -> None:
    source = tmp_path / "trust.pem"
    source.write_bytes(b"fixture trust bundle")
    source.chmod(0o644)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    supplied = {
        "SSL_CERT_FILE": str(source),
        "NODE_EXTRA_CA_CERTS": str(source),
        "HTTPS_PROXY": "https://proxy.invalid",
    }

    result = snapshot_certificate_files(state, supplied, sandbox_root="/sandbox")

    assert supplied["SSL_CERT_FILE"] == str(source)
    assert stat.S_IMODE(source.stat().st_mode) == 0o644
    assert result["HTTPS_PROXY"] == supplied["HTTPS_PROXY"]
    for name, filename in (
        ("SSL_CERT_FILE", "ssl-cert-file.pem"),
        ("NODE_EXTRA_CA_CERTS", "node-extra-ca-certs.pem"),
    ):
        assert result[name] == f"/sandbox/{filename}"
        copied = state / filename
        assert copied.read_bytes() == source.read_bytes()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert copied.stat().st_ino != source.stat().st_ino


@pytest.mark.parametrize("invalid", ["empty", "oversized", "directory"])
def test_invalid_bundle_is_refused_without_exposing_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    source = tmp_path / "private-trust-source"
    if invalid == "directory":
        source.mkdir()
    else:
        source.write_bytes(b"" if invalid == "empty" else b"x" * 9)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr("ithrl.runner.runtime.certificates.MAX_CERTIFICATE_BUNDLE_BYTES", 8)

    with pytest.raises(ValueError, match="cannot snapshot a configured CA bundle") as refused:
        snapshot_certificate_files(state, {"SSL_CERT_FILE": str(source)}, sandbox_root="/sandbox")

    assert str(source) not in str(refused.value)
    assert not tuple(state.iterdir())
