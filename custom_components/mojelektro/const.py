"""Constants for the Moj Elektro integration.

Register codes come from ``GET /reading-type`` on the Moj Elektro API
(OpenAPI 1.0, https://docs.informatika.si/mojelektro/api/mojelektro-openapi.yaml).
The OpenAPI example ``32.0.2.4.1.2.12.0.0.0.0.0.0.0.0.3.72.0`` is the 15-minute
import quantity (oznaka ``A+``, vrsta ``KOLICINA``). Yearly and monthly balance
uses the 24-hour cumulative registers (vrsta ``STANJE``):

* ``A+_T0`` prejeta delovna energija ET — ``32.0.4.1.1.2.12.0.0.0.0.0.0.0.0.3.72.0``
* ``A-_T0`` oddana delovna energija ET — ``32.0.4.1.19.2.12.0.0.0.0.0.0.0.0.3.72.0``

The integration loads ``/reading-type`` at runtime and falls back to these codes.
"""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "mojelektro"
VERSION = "0.1.0"

CONF_TOKEN = "token"
CONF_USAGE_POINT = "usage_point"
CONF_MANUAL = "__manual__"

API_BASE_URL = "https://api.informatika.si/mojelektro/v1"
API_SPEC_URL = "https://docs.informatika.si/mojelektro/api/"
API_OPENAPI_URL = "https://docs.informatika.si/mojelektro/api/mojelektro-openapi.yaml"
API_TOKEN_HEADER = "X-API-TOKEN"

# The OpenAPI file does not publish a numeric limit. The live API rejects a
# wider span with E_4_000: start and end may be at most 35 days apart.
MAX_RANGE_DAYS = 35
REQUEST_TIMEOUT_SECONDS = 30
UPDATE_INTERVAL = timedelta(hours=6)

# Enotni identifikator merilnega mesta, OpenAPI schema EnotniIdentifikatorMerilnegaMesta.
EIMM_PATTERN = r"^\d{1,2}-\d+$"
# usagePoint is an EIMM, a metering-point GSRN, or a sharing-point GSRN.
USAGE_POINT_PATTERN = r"^[A-Za-z0-9-]{3,40}$"

OZNAKA_IMPORT_STATE = "A+_T0"
OZNAKA_EXPORT_STATE = "A-_T0"

READING_TYPE_IMPORT_STATE = "32.0.4.1.1.2.12.0.0.0.0.0.0.0.0.3.72.0"
READING_TYPE_EXPORT_STATE = "32.0.4.1.19.2.12.0.0.0.0.0.0.0.0.3.72.0"
# 15-minute quantities. Documented by the spec; not summed for the yearly balance.
READING_TYPE_IMPORT_15MIN = "32.0.2.4.1.2.12.0.0.0.0.0.0.0.0.3.72.0"
READING_TYPE_EXPORT_15MIN = "32.0.2.4.19.2.12.0.0.0.0.0.0.0.0.3.72.0"

ATTRIBUTION = "Podatki: Moj Elektro / Informatika d.o.o."
MANUFACTURER = "Informatika d.o.o."
PORTAL_URL = "https://mojelektro.si/"
