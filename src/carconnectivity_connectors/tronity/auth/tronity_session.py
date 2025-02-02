"""
Module implements the WeConnect Session handling.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

import json
import logging

from urllib.parse import parse_qsl, urlparse

import requests
from requests.models import CaseInsensitiveDict

from oauthlib.common import add_params_to_uri, generate_nonce, to_unicode
from oauthlib.oauth2 import InsecureTransportError
from oauthlib.oauth2 import is_secure_transport

from carconnectivity.errors import AuthenticationError, RetrievalError, TemporaryAuthenticationError

from carconnectivity_connectors.tronity.auth.openid_session import OAuth2Session, AccessType

if TYPE_CHECKING:
    from typing import Tuple, Dict


LOG: logging.Logger = logging.getLogger("carconnectivity.connectors.tronity.auth")


class TronitySession(OAuth2Session):
    """
    TronitySession class handles the authentication and session management for Volkswagen's WeConnect service.
    """
    def __init__(self, session_user, cache, **kwargs) -> None:
        super(TronitySession, self).__init__(client_id=session_user.client_id,
                                             refresh_url='OpenIDSession',
                                             scope='read_charge read_battery read_vin read_vehicle_info tronity_charges tronity_charging tronity_control_charging tronity_factsheet,tronity_location tronity_odometer tronity_range tronity_soc tronity_vehicle_data',
                                             redirect_uri='tronity://authenticated',
                                             state=None,
                                             **kwargs)
        self.session_user = session_user
        self.cache = cache


    def login(self):
        super(TronitySession, self).login()
        # fetch tokens from web authentication response
        self.fetch_tokens(token_url='https://api.tronity.tech/authentication')

    def refresh(self) -> None:
        # refresh tokens from refresh endpoint
        self.refresh_tokens(
            'https://api.tronity.tech/authentication',
        )

    def fetch_tokens(
        self,
        token_url,
    ):
        body = dict()
        body["grant_type"] = "app"
        body["client_id"] = self.session_user.client_id
        body["client_secret"] = self.session_user.client_secret
        token_response = self.post(token_url, data=body, allow_redirects=True, access_type=AccessType.NONE)

        if token_response.status_code != requests.codes['created']:
            raise TemporaryAuthenticationError(f'Token could not be fetched due to temporary Tronity failure: {token_response.status_code}')
        # parse token from response body
        token = token_response.json()      
        
        self.token = self.parse_from_body(token_response.text)
        return token

    def refresh_tokens(
        self,
        token_url,
        refresh_token=None,
        auth=None,
        timeout=None,
        headers=None,
        verify=True,
        proxies=None,
        **_
    ):
        """
        Refreshes the authentication tokens using the provided refresh token.
        Args:
            token_url (str): The URL to request new tokens from.
            refresh_token (str, optional): The refresh token to use. Defaults to None.
            auth (tuple, optional): Authentication credentials. Defaults to None.
            timeout (float or tuple, optional): How long to wait for the server to send data before giving up. Defaults to None.
            headers (dict, optional): Headers to include in the request. Defaults to None.
            verify (bool, optional): Whether to verify the server's TLS certificate. Defaults to True.
            proxies (dict, optional): Proxies to use for the request. Defaults to None.
            **_ (dict): Additional arguments.
        Raises:
            ValueError: If no token endpoint is set for auto_refresh.
            InsecureTransportError: If the token URL is not secure.
            AuthenticationError: If the server requests new authorization.
            TemporaryAuthenticationError: If the token could not be refreshed due to a temporary server failure.
            RetrievalError: If the status code from the server is not recognized.
        Returns:
            dict: The new tokens.
        """
        LOG.info('Refreshing tokens')
        if not token_url:
            raise ValueError("No token endpoint set for auto_refresh.")

        if not is_secure_transport(token_url):
            raise InsecureTransportError()


        body = dict()
        body["grant_type"] = "refresh_token"

        # Request new tokens using the refresh token
        token_response = self.post(
            token_url,
            auth=auth,
            data=json.dumps(body),
            timeout=timeout,
            verify=verify,
            withhold_token=False,  # pyright: ignore reportCallIssue
            proxies=proxies,
            access_type=AccessType.ACCESS  # pyright: ignore reportCallIssue
        )
        if token_response.status_code == requests.codes['unauthorized']:
            self.login()
            #raise AuthenticationError('Refreshing tokens failed: Server requests new authorization')
        elif token_response.status_code in (requests.codes['internal_server_error'], requests.codes['service_unavailable'], requests.codes['gateway_timeout']):
            raise TemporaryAuthenticationError('Token could not be refreshed due to temporary Tronity failure: {tokenResponse.status_code}')
        elif token_response.status_code == requests.codes['created']:
            # parse new tokens from response
            self.parse_from_body(token_response.text)
            if self.token is not None and "refresh_token" not in self.token:
                LOG.debug("No new refresh token given. Re-using old.")
                self.token["refresh_token"] = refresh_token
            return self.token
        else:
            raise RetrievalError(f'Status Code from Tronity while refreshing tokens was: {token_response.status_code}')
