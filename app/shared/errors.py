"""Shared domain errors mapped to HTTP responses in app.main."""
from __future__ import annotations


class DomainError(Exception):
    http_status = 400

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


class PermissionDenied(DomainError):
    http_status = 403


class NotFound(DomainError):
    http_status = 404


class Conflict(DomainError):
    http_status = 409


class InvalidTransition(DomainError):
    http_status = 409


class PolicyNotConfigured(DomainError):
    http_status = 422
