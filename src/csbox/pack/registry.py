from csbox.core.protocols import BuildAdapter, Exporter
from csbox.core.registry import Registry

BUILD_ADAPTERS = Registry[BuildAdapter]("build-adapter")
EXPORTERS = Registry[Exporter]("exporter")
