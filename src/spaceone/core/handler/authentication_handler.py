import json
import logging

from spaceone.core import cache
from spaceone.core.connector.space_connector import SpaceConnector
from spaceone.core.auth.jwt import JWTAuthenticator, JWTUtil
from spaceone.core.transaction import get_transaction
from spaceone.core.handler import BaseAuthenticationHandler
from spaceone.core.error import ERROR_AUTHENTICATE_FAILURE, ERROR_REQUIRED_X_DOMAIN_ID

_LOGGER = logging.getLogger(__name__)


class SpaceONEAuthenticationHandler(BaseAuthenticationHandler):
    def __init__(self, handler_config):
        super().__init__(handler_config)
        self.identity_client = None
        self._initialize()

    def _initialize(self) -> None:
        self.identity_conn: SpaceConnector = SpaceConnector(service="identity")

    def verify(self, params: dict) -> None:
        token = self._get_token()
        domain_id = self._extract_domain_id(token)

        token_info = self._authenticate(token, domain_id)

        if token_info.get("typ") == "SYSTEM_TOKEN":
            self._update_system_meta(token_info)
        else:
            owner_type = token_info.get("own")
            if owner_type == "APP":
                api_key_id = token_info.get("jti")
                domain_id = token_info.get("did")
                token_info["permissions"] = self._check_app(api_key_id, domain_id)

            self._update_meta(token_info)

    @cache.cacheable(key="handler:authentication:{domain_id}:public-key", alias="local")
    def _get_public_key(self, domain_id: str) -> str:
        _LOGGER.debug(f"[_get_public_key] get jwk from identity service: {domain_id}")
        response = self.identity_conn.dispatch(
            "Domain.get_public_key", {"domain_id": domain_id}
        )

        return response["public_key"]

    @cache.cacheable(
        key="handler:authentication:{domain_id}:api-key:{api_key_id}", alias="local"
    )
    def _check_app(self, api_key_id, domain_id) -> list:
        _LOGGER.debug(f"[_check_app] check app from identity service: {api_key_id}")
        response = self.identity_conn.dispatch(
            "App.check",
            {
                "api_key_id": api_key_id,
                "domain_id": domain_id,
            },
        )

        return response.get("permissions", [])

    def _authenticate(self, token: str, domain_id: str) -> dict:
        public_key = self._get_public_key(domain_id)
        return JWTAuthenticator(json.loads(public_key)).validate(token)

    @staticmethod
    def _get_token() -> str:
        transaction = get_transaction()
        token = transaction.meta.get("token")
        if not isinstance(token, str) or len(token) == 0:
            raise ERROR_AUTHENTICATE_FAILURE(message="empty token provided.")

        return token

    @staticmethod
    def _extract_domain_id(token):
        try:
            decoded = JWTUtil.unverified_decode(token)
        except Exception:
            _LOGGER.debug(f"[_extract_domain_id] failed to decode token: {token[:10]}")
            raise ERROR_AUTHENTICATE_FAILURE(message="failed to decode token.")

        if domain_id := decoded.get("did"):
            return domain_id
        else:
            raise ERROR_AUTHENTICATE_FAILURE(message="empty domain_id provided.")

    def _update_meta(self, token_info: dict) -> None:
        """
        Args:
            token_info(dict): {
                'iss': 'str',   # issuer (spaceone.identity)
                'rol': 'str',   # role type
                'typ': 'str',   # token type (ACCESS_TOKEN | REFRESH_TOKEN | API_KEY)
                'own': 'str',   # owner (USER | APP)
                'did': 'str',   # domain_id
                'wid': 'str',   # workspace_id, Optional
                'aud': 'str',   # audience (user_id | app_id)
                'exp': 'int',   # expiration time
                'iat': 'int',   # issued at
                'jti': 'str',   # jwt id (token_key | api_key_id), Optional
                'permissions': 'list',  # permissions, Optional
                'ver': 'str',   # jwt version
        """

        token_type = token_info.get("typ")
        role_type = token_info.get("rol")
        owner_type = token_info.get("own")
        audience = token_info.get("aud")
        domain_id = token_info.get("did")
        workspace_id = token_info.get("wid")
        permissions = token_info.get("permissions")
        projects = token_info.get("projects")

        self.transaction.set_meta("authorization.token_type", token_type)
        self.transaction.set_meta("authorization.role_type", role_type)
        self.transaction.set_meta("authorization.owner_type", owner_type)
        self.transaction.set_meta("authorization.domain_id", domain_id)
        self.transaction.set_meta("authorization.audience", audience)
        self.transaction.set_meta("authorization.workspace_id", workspace_id)
        self.transaction.set_meta("authorization.permissions", permissions)
        self.transaction.set_meta("authorization.projects", projects)

        if owner_type == "USER":
            self.transaction.set_meta("authorization.user_id", audience)
        elif owner_type == "APP":
            self.transaction.set_meta("authorization.app_id", audience)

    def _update_system_meta(self, token_info: dict) -> None:
        """
        Args:
            token_info(dict): {
                'iss': 'str',   # issuer (spaceone.identity)
                'typ': 'str',   # token type (SYSTEM_TOKEN)
                'own': 'str',   # owner (SYSTEM)
                'did': 'str',   # domain_id (domain-root)
                'aud': 'str',   # audience (root_domain_user_id)
                'iat': 'int',   # issued at
                'jti': 'str',   # jwt id
                'ver': 'str',   # jwt version
        """

        token_type = token_info.get("typ")
        owner_type = token_info.get("own")
        audience = token_info.get("aud")
        domain_id = self.transaction.get_meta("x_domain_id")
        workspace_id = self.transaction.get_meta("x_workspace_id")

        self.transaction.set_meta("authorization.token_type", token_type)
        self.transaction.set_meta("authorization.role_type", "SYSTEM_TOKEN")
        self.transaction.set_meta("authorization.owner_type", owner_type)
        self.transaction.set_meta("authorization.domain_id", domain_id)
        self.transaction.set_meta("authorization.audience", audience)
        self.transaction.set_meta("authorization.workspace_id", workspace_id)
