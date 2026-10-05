class ConnectorError(Exception):
    code = "connector"
    hint = "Check the connector configuration and try again."

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class AuthError(ConnectorError):
    code = "auth"
    hint = "Check the password in your .env file and that it matches the account SSO password."


class ConfigError(ConnectorError):
    code = "config"
    hint = "Check config.yaml and .env for missing or invalid entries."


class FolderNotFoundError(ConnectorError):
    code = "folder_not_found"
    hint = "List the available folders to see the exact folder names."


class MessageNotFoundError(ConnectorError):
    code = "message_not_found"
    hint = "The message may have been deleted; list the folder to get current UIDs."


class NetworkError(ConnectorError):
    code = "network"
    hint = "Check network connectivity and the IMAP/SMTP host and port in config.yaml."


class SendError(ConnectorError):
    code = "send"
    hint = "The SMTP server rejected the message; check the recipients and try again."
