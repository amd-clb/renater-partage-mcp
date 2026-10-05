from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import dotenv_values

from .errors import ConfigError

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465


@dataclass
class AccountConfig:
    email: str
    imap_host: str
    smtp_host: str
    imap_port: int
    smtp_port: int
    password: str


class Config:
    def __init__(self, accounts: list[AccountConfig], domains: dict[str, str]):
        self.accounts = accounts
        self.domains = domains

    def get_account(self, email: str) -> AccountConfig:
        for account in self.accounts:
            if account.email == email:
                return account
        raise ConfigError(f"unknown account: {email}")


def _domain_of(email: str) -> str:
    if "@" not in email:
        raise ConfigError(f"invalid account email (no domain): {email}")
    return email.rsplit("@", 1)[1].lower()


def load_config(config_path: Path, env_path: Path | None = None) -> Config:
    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text()) or {}

    domains = raw.get("domains") or {}
    if not isinstance(domains, dict) or not domains:
        raise ConfigError("'domains' must be a non-empty mapping of domain -> host")

    imap = raw.get("imap") or {}
    smtp = raw.get("smtp") or {}
    imap_port = int(imap.get("port", DEFAULT_IMAP_PORT))
    smtp_port = int(smtp.get("port", DEFAULT_SMTP_PORT))

    if env_path is None:
        env_path = config_path.parent / ".env"
    env_values = dict(dotenv_values(env_path))

    accounts_raw = raw.get("accounts") or []
    if not accounts_raw:
        raise ConfigError("'accounts' must list at least one account")

    accounts: list[AccountConfig] = []
    for index, entry in enumerate(accounts_raw, start=1):
        email = entry.get("email")
        if not email:
            raise ConfigError(f"account #{index} is missing 'email'")
        domain = _domain_of(email)

        imap_host = entry.get("imap_host") or domains.get(domain)
        smtp_host = entry.get("smtp_host") or domains.get(domain)
        if not imap_host or not smtp_host:
            raise ConfigError(
                f"account {email}: domain '{domain}' not in 'domains' "
                "and no per-account host override"
            )

        var = "RENATER_PASSWORD" if index == 1 else f"RENATER_PASSWORD_{index}"
        password = env_values.get(var)
        if not password:
            raise ConfigError(f"missing password for {email}: set {var} in your .env file")

        accounts.append(
            AccountConfig(
                email=email,
                imap_host=imap_host,
                smtp_host=smtp_host,
                imap_port=int(entry.get("imap_port", imap_port)),
                smtp_port=int(entry.get("smtp_port", smtp_port)),
                password=password,
            )
        )

    return Config(accounts, domains)
