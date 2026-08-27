"""Production-oriented XML-RPC client for AWIA's Odoo integration layer.

The client is intentionally read-focused for the first AWIA implementation step.
It provides authenticated, retried, timeout-bound access to the core Odoo models
used by the Agentic Warehouse Inventory Analyst (AWIA):

- product.product
- stock.quant
- stock.move.line
- mrp.production

Credentials are never logged. In production, prefer an Odoo API key rather than
a user's interactive password.
"""

from __future__ import annotations

import http.client
import logging
import os
import random
import socket
import ssl
import threading
import time
import xmlrpc.client
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


logger = logging.getLogger(__name__)

Domain = Sequence[Any]
Record = Dict[str, Any]


class OdooClientError(RuntimeError):
    """Base exception for Odoo client failures."""


class OdooConfigurationError(OdooClientError):
    """Raised when required client configuration is invalid or incomplete."""


class OdooAuthenticationError(OdooClientError):
    """Raised when Odoo authentication fails."""


class OdooConnectionError(OdooClientError):
    """Raised when the Odoo server cannot be reached reliably."""


class OdooRPCError(OdooClientError):
    """Raised when Odoo returns an XML-RPC application fault."""

    def __init__(self, model: str, method: str, fault_code: int, fault_string: str) -> None:
        self.model = model
        self.method = method
        self.fault_code = fault_code
        self.fault_string = fault_string
        super().__init__(
            f"Odoo RPC fault calling {model}.{method}: "
            f"code={fault_code}, message={fault_string}"
        )


class _TimeoutTransport(xmlrpc.client.Transport):
    """HTTP transport with an explicit socket timeout."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host: str) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(host, timeout=self._timeout)


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS transport with explicit timeout and verified TLS context."""

    def __init__(self, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._timeout = timeout
        self._ssl_context = context

    def make_connection(self, host: str) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(
            host,
            timeout=self._timeout,
            context=self._ssl_context,
        )


@dataclass(frozen=True)
class OdooConnectionConfig:
    """Validated Odoo connection settings."""

    url: str
    database: str
    username: str
    secret: str
    timeout_seconds: float = 20.0
    max_retries: int = 3
    retry_backoff_seconds: float = 0.75
    ca_bundle: Optional[str] = None
    allow_insecure_http: bool = False

    @classmethod
    def from_env(cls) -> "OdooConnectionConfig":
        """Build configuration from environment variables.

        Required:
            ODOO_URL
            ODOO_DB
            ODOO_USERNAME
            ODOO_API_KEY or ODOO_PASSWORD

        Optional:
            ODOO_TIMEOUT_SECONDS
            ODOO_MAX_RETRIES
            ODOO_RETRY_BACKOFF_SECONDS
            ODOO_CA_BUNDLE
            ODOO_ALLOW_INSECURE_HTTP=true|false
        """

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise OdooConfigurationError(
                    f"Required environment variable {name} is not set."
                )
            return value

        api_key = os.getenv("ODOO_API_KEY", "").strip()
        password = os.getenv("ODOO_PASSWORD", "").strip()
        secret = api_key or password
        if not secret:
            raise OdooConfigurationError(
                "Set ODOO_API_KEY (preferred) or ODOO_PASSWORD."
            )

        return cls(
            url=required("ODOO_URL"),
            database=required("ODOO_DB"),
            username=required("ODOO_USERNAME"),
            secret=secret,
            timeout_seconds=float(os.getenv("ODOO_TIMEOUT_SECONDS", "20")),
            max_retries=int(os.getenv("ODOO_MAX_RETRIES", "3")),
            retry_backoff_seconds=float(
                os.getenv("ODOO_RETRY_BACKOFF_SECONDS", "0.75")
            ),
            ca_bundle=os.getenv("ODOO_CA_BUNDLE") or None,
            allow_insecure_http=os.getenv(
                "ODOO_ALLOW_INSECURE_HTTP", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
        )


class OdooWarehouseClient:
    """Secure XML-RPC wrapper for AWIA warehouse reads.

    The class authenticates lazily during construction and exposes bounded,
    retry-aware methods for the Odoo models required by AWIA Step 1.

    ServerProxy objects are guarded by a re-entrant lock because their
    underlying HTTP connection objects should not be shared concurrently
    without coordination.
    """

    PRODUCT_FIELDS: Tuple[str, ...] = (
        "id",
        "default_code",
        "name",
        "product_tmpl_id",
        "active",
        "barcode",
        "categ_id",
        "uom_id",
        "standard_price",
        "qty_available",
        "virtual_available",
        "tracking",
        "write_date",
    )

    QUANT_FIELDS: Tuple[str, ...] = (
        "id",
        "product_id",
        "location_id",
        "lot_id",
        "package_id",
        "owner_id",
        "quantity",
        "reserved_quantity",
        "in_date",
        "write_date",
    )

    MOVE_LINE_FIELDS: Tuple[str, ...] = (
        "id",
        "move_id",
        "picking_id",
        "product_id",
        "product_uom_id",
        "lot_id",
        "lot_name",
        "location_id",
        "location_dest_id",
        "quantity",
        "qty_done",
        "date",
        "state",
        "write_uid",
        "write_date",
    )

    PRODUCTION_FIELDS: Tuple[str, ...] = (
        "id",
        "name",
        "product_id",
        "product_qty",
        "product_uom_id",
        "bom_id",
        "state",
        "date_start",
        "date_finished",
        "date_deadline",
        "location_src_id",
        "location_dest_id",
        "move_raw_ids",
        "move_finished_ids",
        "write_date",
    )

    _TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        url: str,
        database: str,
        username: str,
        secret: str,
        *,
        timeout_seconds: float = 20.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.75,
        ca_bundle: Optional[str] = None,
        allow_insecure_http: bool = False,
    ) -> None:
        self.url = self._validate_url(url, allow_insecure_http)
        self.database = self._require_nonempty(database, "database")
        self.username = self._require_nonempty(username, "username")
        self._secret = self._require_nonempty(secret, "secret")

        if timeout_seconds <= 0:
            raise OdooConfigurationError("timeout_seconds must be greater than zero.")
        if max_retries < 0:
            raise OdooConfigurationError("max_retries cannot be negative.")
        if retry_backoff_seconds < 0:
            raise OdooConfigurationError(
                "retry_backoff_seconds cannot be negative."
            )

        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self._ca_bundle = ca_bundle
        self._lock = threading.RLock()
        self._field_cache: Dict[str, Tuple[str, ...]] = {}

        self._common: xmlrpc.client.ServerProxy
        self._models: xmlrpc.client.ServerProxy
        self._build_proxies()
        self.uid = self._authenticate()

        logger.info(
            "Authenticated to Odoo host=%s database=%s username=%s uid=%s",
            urlsplit(self.url).netloc,
            self.database,
            self.username,
            self.uid,
        )

    @classmethod
    def from_config(cls, config: OdooConnectionConfig) -> "OdooWarehouseClient":
        return cls(
            url=config.url,
            database=config.database,
            username=config.username,
            secret=config.secret,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            retry_backoff_seconds=config.retry_backoff_seconds,
            ca_bundle=config.ca_bundle,
            allow_insecure_http=config.allow_insecure_http,
        )

    @classmethod
    def from_env(cls) -> "OdooWarehouseClient":
        return cls.from_config(OdooConnectionConfig.from_env())

    @staticmethod
    def _require_nonempty(value: str, label: str) -> str:
        cleaned = value.strip() if isinstance(value, str) else ""
        if not cleaned:
            raise OdooConfigurationError(f"{label} must be a non-empty string.")
        return cleaned

    @staticmethod
    def _validate_url(url: str, allow_insecure_http: bool) -> str:
        cleaned = url.strip().rstrip("/")
        parsed = urlsplit(cleaned)

        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OdooConfigurationError(
                "url must be an absolute http:// or https:// URL."
            )
        if parsed.username or parsed.password:
            raise OdooConfigurationError(
                "Do not embed credentials in ODOO_URL; use dedicated secret settings."
            )
        if parsed.scheme == "http" and not allow_insecure_http:
            raise OdooConfigurationError(
                "Plain HTTP is disabled. Use HTTPS or explicitly set "
                "allow_insecure_http=True for a trusted development environment."
            )
        return cleaned

    def _build_proxies(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme == "https":
            ssl_context = ssl.create_default_context(cafile=self._ca_bundle)
            transport: xmlrpc.client.Transport = _TimeoutSafeTransport(
                self.timeout_seconds, ssl_context
            )
        else:
            transport = _TimeoutTransport(self.timeout_seconds)

        common_endpoint = f"{self.url}/xmlrpc/2/common"
        object_endpoint = f"{self.url}/xmlrpc/2/object"

        self._common = xmlrpc.client.ServerProxy(
            common_endpoint,
            transport=transport,
            allow_none=True,
            use_builtin_types=True,
        )

        # Use a separate transport so the common and object proxies do not
        # share a single persistent HTTP connection object.
        if parsed.scheme == "https":
            object_transport: xmlrpc.client.Transport = _TimeoutSafeTransport(
                self.timeout_seconds,
                ssl.create_default_context(cafile=self._ca_bundle),
            )
        else:
            object_transport = _TimeoutTransport(self.timeout_seconds)

        self._models = xmlrpc.client.ServerProxy(
            object_endpoint,
            transport=object_transport,
            allow_none=True,
            use_builtin_types=True,
        )

    def _backoff(self, attempt: int) -> None:
        if self.retry_backoff_seconds == 0:
            return
        base = self.retry_backoff_seconds * (2**attempt)
        jitter = random.uniform(0, base * 0.20)
        time.sleep(base + jitter)

    @staticmethod
    def _is_retryable_protocol_error(exc: xmlrpc.client.ProtocolError) -> bool:
        return exc.errcode in OdooWarehouseClient._TRANSIENT_HTTP_CODES

    def _call_with_retry(self, operation: str, func: Any) -> Any:
        last_exception: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            try:
                with self._lock:
                    return func()
            except xmlrpc.client.Fault:
                # Odoo application faults are deterministic for the submitted
                # request and should not be retried automatically.
                raise
            except xmlrpc.client.ProtocolError as exc:
                last_exception = exc
                retryable = self._is_retryable_protocol_error(exc)
                if not retryable or attempt >= self.max_retries:
                    break
                logger.warning(
                    "Transient Odoo HTTP error during %s: status=%s attempt=%s/%s",
                    operation,
                    exc.errcode,
                    attempt + 1,
                    self.max_retries + 1,
                )
            except (socket.timeout, TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
                last_exception = exc
                if attempt >= self.max_retries:
                    break
                logger.warning(
                    "Transient Odoo connection error during %s: %s attempt=%s/%s",
                    operation,
                    exc.__class__.__name__,
                    attempt + 1,
                    self.max_retries + 1,
                )

            self._backoff(attempt)
            # Recreate connections after a transport-level failure so a stale
            # keep-alive socket is not reused on the next attempt.
            with self._lock:
                self._build_proxies()

        detail = (
            f"{last_exception.__class__.__name__}: {last_exception}"
            if last_exception is not None
            else "unknown connection failure"
        )
        raise OdooConnectionError(
            f"Odoo operation '{operation}' failed after "
            f"{self.max_retries + 1} attempt(s): {detail}"
        ) from last_exception

    def _authenticate(self) -> int:
        def authenticate() -> Any:
            # Calling version() first produces a clearer network/protocol error
            # before credentials are evaluated.
            self._common.version()
            return self._common.authenticate(
                self.database,
                self.username,
                self._secret,
                {},
            )

        try:
            uid = self._call_with_retry("authenticate", authenticate)
        except xmlrpc.client.Fault as exc:
            raise OdooAuthenticationError(
                f"Odoo rejected the authentication request: {exc.faultString}"
            ) from exc

        if not isinstance(uid, int) or uid <= 0:
            raise OdooAuthenticationError(
                "Authentication failed. Verify database, username, and API key/password."
            )
        return uid

    def execute_kw(
        self,
        model: str,
        method: str,
        args: Optional[Sequence[Any]] = None,
        kwargs: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Execute an authenticated Odoo model method.

        This method is intentionally low-level. AWIA service code should favor
        the model-specific read methods below. Any future mutating workflow
        should be wrapped in an explicit HITL approval service rather than
        called directly from an autonomous agent.
        """
        model = self._require_nonempty(model, "model")
        method = self._require_nonempty(method, "method")
        rpc_args = list(args or [])
        rpc_kwargs = dict(kwargs or {})

        def invoke() -> Any:
            return self._models.execute_kw(
                self.database,
                self.uid,
                self._secret,
                model,
                method,
                rpc_args,
                rpc_kwargs,
            )

        try:
            return self._call_with_retry(f"{model}.{method}", invoke)
        except xmlrpc.client.Fault as exc:
            raise OdooRPCError(
                model=model,
                method=method,
                fault_code=exc.faultCode,
                fault_string=exc.faultString,
            ) from exc

    def check_read_access(self, model: str) -> bool:
        return bool(
            self.execute_kw(
                model,
                "check_access_rights",
                args=["read"],
                kwargs={"raise_exception": False},
            )
        )

    def available_fields(self, model: str) -> Tuple[str, ...]:
        """Return readable field names for a model, cached per client.

        The cache enables one client implementation to tolerate normal field
        differences across Odoo 15/16/17 deployments.
        """
        if model in self._field_cache:
            return self._field_cache[model]

        metadata = self.execute_kw(
            model,
            "fields_get",
            args=[],
            kwargs={"attributes": ["type"]},
        )
        if not isinstance(metadata, dict):
            raise OdooRPCError(
                model=model,
                method="fields_get",
                fault_code=-1,
                fault_string="Unexpected fields_get response type.",
            )

        fields = tuple(metadata.keys())
        self._field_cache[model] = fields
        return fields

    def _resolve_fields(
        self,
        model: str,
        requested_fields: Optional[Sequence[str]],
        preferred_fields: Sequence[str],
    ) -> List[str]:
        candidates = list(requested_fields or preferred_fields)
        if not candidates:
            raise OdooConfigurationError(
                f"At least one field must be requested for model {model}."
            )

        available = set(self.available_fields(model))
        selected = [field for field in candidates if field in available]
        missing = [field for field in candidates if field not in available]

        if missing:
            logger.info(
                "Skipping unavailable Odoo fields model=%s fields=%s",
                model,
                missing,
            )
        if not selected:
            raise OdooConfigurationError(
                f"None of the requested fields are available on model {model}."
            )
        return selected

    def search_read(
        self,
        model: str,
        *,
        domain: Optional[Domain] = None,
        fields: Sequence[str],
        limit: Optional[int] = 500,
        offset: int = 0,
        order: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> List[Record]:
        if limit is not None and limit <= 0:
            raise OdooConfigurationError("limit must be positive or None.")
        if offset < 0:
            raise OdooConfigurationError("offset cannot be negative.")

        kwargs: Dict[str, Any] = {
            "fields": list(fields),
            "offset": offset,
        }
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        if context:
            kwargs["context"] = dict(context)

        result = self.execute_kw(
            model,
            "search_read",
            args=[list(domain or [])],
            kwargs=kwargs,
        )
        if not isinstance(result, list):
            raise OdooRPCError(
                model=model,
                method="search_read",
                fault_code=-1,
                fault_string="Unexpected search_read response type.",
            )
        return result

    def iter_search_read(
        self,
        model: str,
        *,
        domain: Optional[Domain] = None,
        fields: Sequence[str],
        batch_size: int = 500,
        order: str = "id asc",
        context: Optional[Mapping[str, Any]] = None,
    ) -> Iterator[Record]:
        """Yield records in bounded pages for large hourly sync jobs."""
        if batch_size <= 0:
            raise OdooConfigurationError("batch_size must be greater than zero.")

        offset = 0
        while True:
            page = self.search_read(
                model,
                domain=domain,
                fields=fields,
                limit=batch_size,
                offset=offset,
                order=order,
                context=context,
            )
            if not page:
                return

            yield from page

            if len(page) < batch_size:
                return
            offset += len(page)

    def fetch_products(
        self,
        *,
        domain: Optional[Domain] = None,
        fields: Optional[Sequence[str]] = None,
        limit: Optional[int] = 500,
        offset: int = 0,
        order: str = "id asc",
    ) -> List[Record]:
        selected = self._resolve_fields(
            "product.product", fields, self.PRODUCT_FIELDS
        )
        return self.search_read(
            "product.product",
            domain=domain,
            fields=selected,
            limit=limit,
            offset=offset,
            order=order,
        )

    def fetch_stock_quants(
        self,
        *,
        domain: Optional[Domain] = None,
        fields: Optional[Sequence[str]] = None,
        limit: Optional[int] = 500,
        offset: int = 0,
        order: str = "id asc",
    ) -> List[Record]:
        selected = self._resolve_fields("stock.quant", fields, self.QUANT_FIELDS)
        return self.search_read(
            "stock.quant",
            domain=domain,
            fields=selected,
            limit=limit,
            offset=offset,
            order=order,
        )

    def fetch_stock_move_lines(
        self,
        *,
        domain: Optional[Domain] = None,
        fields: Optional[Sequence[str]] = None,
        limit: Optional[int] = 500,
        offset: int = 0,
        order: str = "id asc",
    ) -> List[Record]:
        selected = self._resolve_fields(
            "stock.move.line", fields, self.MOVE_LINE_FIELDS
        )
        return self.search_read(
            "stock.move.line",
            domain=domain,
            fields=selected,
            limit=limit,
            offset=offset,
            order=order,
        )

    def fetch_manufacturing_orders(
        self,
        *,
        domain: Optional[Domain] = None,
        fields: Optional[Sequence[str]] = None,
        limit: Optional[int] = 500,
        offset: int = 0,
        order: str = "id asc",
    ) -> List[Record]:
        selected = self._resolve_fields(
            "mrp.production", fields, self.PRODUCTION_FIELDS
        )
        return self.search_read(
            "mrp.production",
            domain=domain,
            fields=selected,
            limit=limit,
            offset=offset,
            order=order,
        )


def _demo() -> None:
    """Connectivity smoke test; safe because it performs reads only."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    client = OdooWarehouseClient.from_env()

    # Limit the smoke test so executing this module never performs an
    # accidental full-table extraction from a production ERP instance.
    products = client.fetch_products(
        domain=[["active", "=", True]],
        limit=5,
    )
    logger.info("Odoo connectivity smoke test returned %d product(s).", len(products))


if __name__ == "__main__":
    _demo()
