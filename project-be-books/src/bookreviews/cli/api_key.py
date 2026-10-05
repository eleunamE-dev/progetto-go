import re
import sys

from bookreviews.api.auth import key_digest, new_api_key
from bookreviews.config import CLIENT_NAME_PATTERN

CLIENT_NAME = re.compile(CLIENT_NAME_PATTERN)


def main() -> None:
    match sys.argv[1:]:
        case [client] if CLIENT_NAME.fullmatch(client):
            key = new_api_key()
            sys.stdout.write(
                f"API key for {client}; hand it to the client, it is not stored anywhere:\n"
                f"  {key}\n"
                "Entry to add to API_KEYS:\n"
                f'  "{client}": "{key_digest(key)}"\n'
            )
        case _:
            sys.exit(
                "usage: bookreviews-api-key CLIENT\n"
                "CLIENT: up to 63 lowercase letters, digits and hyphens, not starting with a hyphen"
            )
