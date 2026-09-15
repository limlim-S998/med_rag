"""Configure process telemetry at startup; request middleware is attached separately."""

from medw_core import metrics, tracing
from medw_core.provenance import Provenance
from medw_core.settings import Settings


def configure_telemetry(settings: Settings, provenance: Provenance) -> None:
    """Give logs, metrics and traces the same effective process identity."""
    resource = provenance.as_telemetry_resource()
    connection = settings.appinsights_connection_string or ""
    tracing.configure_logging(settings.log_level, provenance.service)
    metrics.configure(connection, provenance.service, resource_attributes=resource)
    tracing.configure(provenance.service, connection, resource_attributes=resource)
