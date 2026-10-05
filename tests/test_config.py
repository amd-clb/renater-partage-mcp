from pathlib import Path

import pytest

from src.config import load_config
from src.errors import ConfigError


def make_config(tmp_path: Path, accounts_yaml: str, domains_yaml: str) -> Path:
    body = f"accounts:\n{accounts_yaml}\ndomains:\n{domains_yaml}\nimap: {{ port: 993, ssl: true }}\nsmtp: {{ port: 465, ssl: true }}\n"
    p = tmp_path / "config.yaml"
    p.write_text(body)
    env = tmp_path / ".env"
    env.write_text("RENATER_PASSWORD=secret\n")
    return p


def test_load_config_readonly_flag_defaults_false(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n",
    )
    assert load_config(p, tmp_path / ".env").readonly is False


def test_load_config_readonly_flag_true(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n",
    )
    p.write_text(p.read_text() + "readonly: true\n")
    assert load_config(p, tmp_path / ".env").readonly is True


def test_load_config_resolves_hosts(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n  example2.edu: mail.example.edu\n",
    )
    cfg = load_config(p, tmp_path / ".env")
    acc = cfg.get_account("prenom.nom@example.edu")
    assert acc.imap_host == "mail.example.edu"
    assert acc.smtp_host == "mail.example.edu"
    assert acc.imap_port == 993
    assert acc.smtp_port == 465
    assert acc.password == "secret"


def test_load_config_readonly_flag(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n",
    )
    assert load_config(p).readonly is False
    p.write_text(p.read_text() + "readonly: true\n")
    assert load_config(p).readonly is True


def test_load_config_defaults_to_env_next_to_config(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n",
    )
    cfg = load_config(p)
    assert cfg.get_account("prenom.nom@example.edu").password == "secret"


def test_account_domain_missing_from_map_raises(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: x@unknown.fr\n",
        "  example.edu: mail.example.edu\n",
    )
    with pytest.raises(ConfigError):
        load_config(p, tmp_path / ".env")


def test_get_account_unknown_raises(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n",
    )
    cfg = load_config(p, tmp_path / ".env")
    with pytest.raises(ConfigError):
        cfg.get_account("nobody@nowhere.fr")


def test_per_account_host_override(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n    imap_host: mail.example.fr\n",
        "  example.edu: mail.example.edu\n",
    )
    cfg = load_config(p, tmp_path / ".env")
    assert cfg.get_account("prenom.nom@example.edu").imap_host == "mail.example.fr"


def test_password_missing_raises(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n",
        "  example.edu: mail.example.edu\n",
    )
    (tmp_path / ".env").write_text("")
    with pytest.raises(ConfigError, match="RENATER_PASSWORD"):
        load_config(p, tmp_path / ".env")


def test_second_account_password_indexed(tmp_path):
    p = make_config(
        tmp_path,
        "  - email: prenom.nom@example.edu\n  - email: prenom.nom@example2.edu\n",
        "  example.edu: mail.example.edu\n  example2.edu: mail.example.edu\n",
    )
    (tmp_path / ".env").write_text("RENATER_PASSWORD=one\nRENATER_PASSWORD_2=two\n")
    cfg = load_config(p, tmp_path / ".env")
    assert cfg.get_account("prenom.nom@example.edu").password == "one"
    assert cfg.get_account("prenom.nom@example2.edu").password == "two"
