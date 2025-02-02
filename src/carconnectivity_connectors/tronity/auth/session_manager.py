"""Module implementing the SessionManager class."""
from __future__ import annotations
from typing import TYPE_CHECKING, Tuple

from enum import Enum

import hashlib

import logging

from carconnectivity_connectors.tronity.auth.tronity_session import TronitySession

if TYPE_CHECKING:
    from typing import Dict, Any

LOG = logging.getLogger("carconnectivity.connectors.tronity.auth")


class SessionCredentials():
    """
    A class to represent a session client with a client_id and client_secret.

    Methods:
    -------
    __str__():
        Returns a string representation of the session user in the format 'username:password'.
    """
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id: str = client_id
        self.client_secret: str = client_secret

    def __str__(self) -> str:
        return f'{self.client_id}:{self.client_secret}'


class Service(Enum):
    """
    An enumeration representing different services.
    """
    TRONITY = 'Tronity'

    def __str__(self) -> str:
        return self.value


class SessionManager():
    """
    Manages sessions for different services and users, handling token storage and caching.
    """
    def __init__(self, tokenstore: Dict[str, Any], cache:  Dict[str, Any]) -> None:
        self.tokenstore: Dict[str, Any] = tokenstore
        self.cache: Dict[str, Any] = cache
        self.sessions: Dict[Tuple[Service, SessionCredentials], TronitySession] = {}

    @staticmethod
    def generate_hash(service: Service, session_user: SessionCredentials) -> str:
        """
        Generates a SHA-512 hash for the given service and session user.

        Args:
            service (Service): The service for which the hash is being generated.
            session_user (SessionCredentials): The session user for which the hash is being generated.

        Returns:
            str: The generated SHA-512 hash as a hexadecimal string.
        """
        hash_str: str = service.value + str(session_user)
        return hashlib.sha512(hash_str.encode()).hexdigest()

    @staticmethod
    def generate_identifier(service: Service, session_user: SessionCredentials) -> str:
        """
        Generate a unique identifier for a given service and session user.

        Args:
            service (Service): The service for which the identifier is being generated.
            session_user (SessionCredentials): The session user for whom the identifier is being generated.

        Returns:
            str: A unique identifier string.
        """
        return 'CarConnectivity-connector-tronity:' + SessionManager.generate_hash(service, session_user)

    def get_session(self, service: Service, session_user: SessionCredentials) -> TronitySession:
        """
        Retrieves a session for the given service and session user. If a session already exists in the sessions cache,
        it is returned. Otherwise, a new session is created using the token, metadata, and cache from the tokenstore
        and cache if available.

        Args:
            service (Service): The service for which the session is being requested.
            session_user (SessionCredentials): The user for whom the session is being requested.

        Returns:
            Session: The session object for the given service and session user.
        """
        session = None
        if (service, session_user) in self.sessions:
            return self.sessions[(service, session_user)]

        identifier: str = SessionManager.generate_identifier(service, session_user)
        token = None
        cache = {}
        metadata = {}

        if identifier in self.tokenstore:
            if 'token' in self.tokenstore[identifier]:
                LOG.info('Reusing tokens from previous session')
                token = self.tokenstore[identifier]['token']
            if 'metadata' in self.tokenstore[identifier]:
                metadata = self.tokenstore[identifier]['metadata']
        if identifier in self.cache:
            cache = self.cache[identifier]

        if service == Service.TRONITY:
            session = TronitySession(session_user=session_user, token=token, metadata=metadata, cache=cache)
        else:
            raise ValueError(f"Unsupported service: {service}")

        self.sessions[(service, session_user)] = session
        return session

    def persist(self) -> None:
        """
        Persist the current sessions into the token store and cache.

        This method iterates over the sessions and stores each session's token and metadata
        in the token store using a generated identifier. It also stores the session's cache
        in the cache.
        """
        for (service, user), session in self.sessions.items():
            identifier: str = SessionManager.generate_identifier(service, user)
            self.tokenstore[identifier] = {}
            self.tokenstore[identifier]['token'] = session.token
            self.tokenstore[identifier]['metadata'] = session.metadata
            self.cache[identifier] = session.cache
