"""Django AppConfig registering the core media content type."""

from strata_cms.contrib.media.schema import MEDIA_PLUGIN, MEDIA_TYPE
from strata_cms.plugin_api.django import StrataPluginConfig
from strata_cms.plugin_api.registry import PluginRegistrar


class MediaPluginConfig(StrataPluginConfig):
    """Core plugin providing the ``strata.media`` content type."""

    name = "strata_cms.contrib.media"
    label = "strata_media"
    strata_plugin = MEDIA_PLUGIN

    def register_strata(self, registrar: PluginRegistrar) -> None:
        """Register the media content type through the scoped registrar."""
        registrar.register_content_type(MEDIA_TYPE)
