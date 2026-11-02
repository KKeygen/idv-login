import base64
import hashlib
import uuid as _uuid
import secrets
from urllib.parse import urlencode

from channelHandler.honorLogin.consts import HONOR_OAUTH_BASE, HONOR_REDIRECT_URI


def generate_code_challenge(code_verifier: str) -> str:
    sha256 = hashlib.sha256()
    sha256.update(code_verifier.encode("ascii"))
    return base64.urlsafe_b64encode(sha256.digest()).decode("ascii").rstrip("=")


def get_authorization_code(client_id: str, redirect_uri: str = HONOR_REDIRECT_URI, scope: str = ""):
    """构建荣耀 OAuth 授权 URL，返回 (auth_url, code_verifier)。"""
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = generate_code_challenge(code_verifier)
    nonce = str(_uuid.uuid4())
    state = str(_uuid.uuid4())

    auth_url = f"{HONOR_OAUTH_BASE}/oauth2/v3/authorize?" + urlencode({
        "access_type": "offline",
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
        "display": "touch",
        "state": state,
    })
    return auth_url, code_verifier
