"""Async client for the Moj Elektro API.

Spec: https://docs.informatika.si/mojelektro/api/
OpenAPI: https://docs.informatika.si/mojelektro/api/mojelektro-openapi.yaml

Production server: https://api.informatika.si/mojelektro/v1
Authentication: header ``X-API-TOKEN`` (security scheme ``X-API-TOKEN``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from .balance import Reading, parse_meter_readings
from .const import (
    API_BASE_URL,
    API_TOKEN_HEADER,
    EIMM_PATTERN,
    MAX_RANGE_DAYS,
    READING_TYPE_IMPORT_STATE,
    REQUEST_TIMEOUT_SECONDS,
    USAGE_POINT_PATTERN,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

_EIMM_RE = re.compile(EIMM_PATTERN)
_USAGE_POINT_RE = re.compile(USAGE_POINT_PATTERN)


class MojElektroError(Exception):
    """Base error for the Moj Elektro API."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class MojElektroAuthError(MojElektroError):
    """HTTP 401 or 403: the token is missing or does not grant access."""


class MojElektroTimeoutError(MojElektroError):
    """The API call timed out."""


class MojElektroConnectionError(MojElektroError):
    """The API host could not be reached."""


class MojElektroApiError(MojElektroError):
    """The API returned an error payload or an unexpected document."""


class MojElektroNotFoundError(MojElektroApiError):
    """The metering point does not exist or the identifier is rejected."""


@dataclass(frozen=True)
class MeterInfo:
    """Name and identifiers returned for a metering point."""

    usage_point: str
    name: str | None
    address: str | None
    gsrn: str | None


class MojElektroClient:
    """Small aiohttp wrapper around the endpoints this integration calls."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        *,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        self._headers = {
            API_TOKEN_HEADER: token,
            "Accept": "application/json",
            "User-Agent": f"HomeAssistant-mojelektro/{VERSION}",
        }

    def __repr__(self) -> str:
        return f"MojElektroClient(base_url={self._base_url!r})"

    async def get_reading_types(self) -> list[Any]:
        """Return the ``GET /reading-type`` catalog."""
        payload = await self._request("/reading-type")
        if not isinstance(payload, list):
            raise MojElektroApiError("Unexpected reading-type payload")
        return payload

    async def get_merilno_mesto(self, identifikator: str) -> dict[str, Any]:
        """Return ``GET /merilno-mesto/{identifikator}``."""
        payload = await self._request(
            "/merilno-mesto/" + quote(identifikator, safe="-")
        )
        if not isinstance(payload, dict):
            raise MojElektroApiError("Unexpected merilno-mesto payload")
        return payload

    async def get_merilna_tocka(self, gsrn: str) -> dict[str, Any]:
        """Return ``GET /merilna-tocka/{gsrn}``."""
        payload = await self._request("/merilna-tocka/" + quote(gsrn, safe=""))
        if not isinstance(payload, dict):
            raise MojElektroApiError("Unexpected merilna-tocka payload")
        return payload

    async def get_meter_readings(
        self,
        usage_point: str,
        start: date,
        end: date,
        reading_types: list[str],
    ) -> dict[str, list[Reading]]:
        """Return parsed ``GET /meter-readings`` for one inclusive date window.

        ``option`` is repeated, once per ``ReadingType=...`` value, because the
        OpenAPI schema types it as an array of ``key=value`` strings.
        """
        _check_range(start, end)
        if not reading_types:
            raise MojElektroApiError("At least one ReadingType is required")
        params: list[tuple[str, str]] = [
            ("usagePoint", usage_point),
            ("startTime", start.isoformat()),
            ("endTime", end.isoformat()),
        ]
        for reading_type in dict.fromkeys(reading_types):
            params.append(("option", f"ReadingType={reading_type}"))
        payload = await self._request("/meter-readings", params)
        if not isinstance(payload, dict):
            raise MojElektroApiError("Unexpected meter-readings payload")
        return parse_meter_readings(payload)

    async def discover_usage_points(self) -> list[str]:
        """Return EIMM values found on ``GET /souporaba``.

        The API has no method that lists every metering point on the token.
        Sharing applications (``eiMmOddajnika`` / ``eiMmPrejemnika``) are the
        only collection the spec exposes. HTTP 403/404 means this token has no
        sharing data and yields an empty list. HTTP 401 still fails auth.
        """
        try:
            payload = await self._request("/souporaba")
        except MojElektroAuthError as err:
            if err.status == 403:
                return []
            raise
        except MojElektroNotFoundError:
            return []
        return extract_usage_points(payload)

    async def validate_usage_point(self, usage_point: str, *, today: date) -> MeterInfo:
        """Confirm the token can read this usage point and return its name."""
        usage_point = usage_point.strip()
        if _USAGE_POINT_RE.fullmatch(usage_point) is None:
            raise MojElektroNotFoundError("Invalid usage point")

        if _EIMM_RE.fullmatch(usage_point):
            try:
                payload = await self.get_merilno_mesto(usage_point)
            except MojElektroApiError as err:
                if err.status in (400, 404):
                    raise MojElektroNotFoundError(str(err), status=err.status) from err
                raise
            return meter_info_from_merilno_mesto(payload, usage_point)

        start = today - timedelta(days=1)
        try:
            await self.get_meter_readings(
                usage_point,
                start,
                today,
                [READING_TYPE_IMPORT_STATE],
            )
        except MojElektroApiError as err:
            if err.status in (400, 404):
                raise MojElektroNotFoundError(str(err), status=err.status) from err
            raise

        name: str | None = None
        if usage_point.isdigit():
            try:
                point = await self.get_merilna_tocka(usage_point)
            except MojElektroError as err:
                _LOGGER.debug("Merilna tocka lookup failed: %s", err)
            else:
                raw_name = point.get("naziv")
                if isinstance(raw_name, str) and raw_name.strip():
                    name = raw_name.strip()
        return MeterInfo(
            usage_point=usage_point,
            name=name,
            address=None,
            gsrn=usage_point,
        )

    async def _request(
        self,
        path: str,
        params: list[tuple[str, str]] | None = None,
    ) -> Any:
        url = self._base_url + "/" + path.lstrip("/")
        try:
            async with self._session.get(
                url,
                headers=self._headers,
                params=params,
                timeout=self._timeout,
            ) as response:
                raw = await response.text()
                status = response.status
        except (asyncio.TimeoutError, TimeoutError) as err:
            raise MojElektroTimeoutError("Moj Elektro API timed out") from err
        except aiohttp.ClientError as err:
            raise MojElektroConnectionError("Could not reach Moj Elektro API") from err

        payload = _decode_json(raw, status)
        if status in (401, 403):
            raise MojElektroAuthError(
                error_message(payload) or f"HTTP {status}",
                status=status,
            )
        if status == 404:
            raise MojElektroNotFoundError(
                error_message(payload) or "Not found",
                status=status,
            )
        if status >= 400:
            raise MojElektroApiError(
                error_message(payload) or f"HTTP {status}",
                status=status,
            )
        return payload


def meter_info_from_merilno_mesto(payload: dict[str, Any], usage_point: str) -> MeterInfo:
    """Read name, address and GSRN from a ``MerilnoMestoPolno`` document."""
    ident = payload.get("identifikator")
    gsrn = ident.get("gsrn") if isinstance(ident, dict) else None
    name = payload.get("naziv")
    address = payload.get("naslov")
    return MeterInfo(
        usage_point=usage_point,
        name=name.strip() if isinstance(name, str) and name.strip() else None,
        address=address.strip() if isinstance(address, str) and address.strip() else None,
        gsrn=gsrn.strip() if isinstance(gsrn, str) and gsrn.strip() else None,
    )


def extract_usage_points(payload: Any) -> list[str]:
    """Collect unique EIMM values from a ``Souporaba`` list."""
    if not isinstance(payload, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        for key in ("eiMmOddajnika", "eiMmPrejemnika"):
            value = _eimm(item.get(key))
            if value and value not in seen:
                seen.add(value)
                found.append(value)
    return found


def error_message(payload: Any) -> str | None:
    """Read ``opis`` from a ``Napaka`` object or an ``errors`` list.

    The OpenAPI response schema is ``Napaka`` (``koda`` + ``opis``). Live
    responses also use the ``OpisNapake`` shape with an ``errors`` array.
    """
    if not isinstance(payload, dict):
        return None
    opis = payload.get("opis")
    if isinstance(opis, str) and opis.strip():
        koda = payload.get("koda")
        if isinstance(koda, str) and koda.strip():
            return f"{koda}: {opis.strip()}"
        return opis.strip()
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    parts: list[str] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        text = item.get("opis")
        if not isinstance(text, str) or not text.strip():
            continue
        koda = item.get("koda")
        if isinstance(koda, str) and koda.strip():
            parts.append(f"{koda}: {text.strip()}")
        else:
            parts.append(text.strip())
    if not parts:
        return None
    return "; ".join(parts)


def _eimm(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        nested = value.get("enotniIdentifikatorMerilnegaMesta")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _decode_json(raw: str, status: int) -> Any:
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise MojElektroApiError(f"Invalid JSON (HTTP {status})", status=status) from err


def _check_range(start: date, end: date) -> None:
    if end < start:
        raise MojElektroApiError("endTime is before startTime")
    if (end - start).days > MAX_RANGE_DAYS:
        raise MojElektroApiError(
            f"Interval {start.isoformat()} to {end.isoformat()} exceeds "
            f"{MAX_RANGE_DAYS} days"
        )
