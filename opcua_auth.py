from __future__ import annotations

import logging
import types
from typing import Any

LOGGER = logging.getLogger(__name__)


def patch_blank_basic256_username_token(
    client: Any,
    username: str,
    password: str,
    policy_id: str = "3",
    policy_uri: str = "http://opcfoundation.org/UA/SecurityPolicy#Basic256",
) -> None:
    client.set_user(username)
    client.set_password(password)

    def _add_user_auth(self: Any, params: Any, server_nonce: bytes) -> None:
        params.UserIdentityToken.PolicyId = policy_id
        params.UserIdentityToken.UserName = username
        blank_password = password.encode("utf-8")
        params.UserIdentityToken.Password = self._encrypt_password(blank_password, policy_uri)
        params.UserIdentityToken.EncryptionAlgorithm = policy_uri

    client._add_user_auth = types.MethodType(_add_user_auth, client)
    LOGGER.debug(
        "Applied blank-password Basic256 username token patch for user %s with policy_id=%s",
        username,
        policy_id,
    )
