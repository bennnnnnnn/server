"""Models for providers and plugins in the MA ecosystem."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, TypedDict

from mashumaro.mixins.orjson import DataClassORJSONMixin

from music_assistant.common.helpers.json import load_json_file

from .enums import MediaType, ProviderFeature, ProviderType


@dataclass
class ProviderManifest(DataClassORJSONMixin):
    """ProviderManifest, details of a provider."""

    type: ProviderType
    domain: str
    name: str
    description: str
    codeowners: list[str]

    # optional params

    # requirements: list of (pip style) python packages required for this provider
    requirements: list[str] = field(default_factory=list)
    # documentation: link/url to documentation.
    documentation: str | None = None
    # multi_instance: whether multiple instances of the same provider are allowed/possible
    multi_instance: bool = False
    # builtin: whether this provider is a system/builtin and can not disabled/removed
    builtin: bool = False
    # load_by_default: load this provider by default (mostly used together with `builtin`)
    load_by_default: bool = False
    # depends_on: depends on another provider to function
    depends_on: str | None = None
    # icon: icon url (svg or transparent png) max 256 pixels
    # may also be a direct base64 encoded image string
    # if this attribute is omitted and an icon.svg or icon.png is found in the provider
    # folder, it will be read instead.
    icon: str | None = None

    @classmethod
    async def parse(cls: "ProviderManifest", manifest_file: str) -> "ProviderManifest":
        """Parse ProviderManifest from file."""
        return await load_json_file(manifest_file, ProviderManifest)


class ProviderInstance(TypedDict):
    """Provider instance detailed dict when a provider is serialized over the api."""

    type: ProviderType
    domain: str
    name: str
    instance_id: str
    supported_features: list[ProviderFeature]
    available: bool
    icon: str | None


@dataclass
class SyncTask:
    """Description of a Sync task/job of a musicprovider."""

    provider_domain: str
    provider_instance: str
    media_types: tuple[MediaType, ...]
    task: asyncio.Task

    def to_dict(self, *args, **kwargs) -> dict[str, Any]:
        """Return SyncTask as (serializable) dict."""
        # ruff: noqa:ARG002
        return {
            "provider_domain": self.provider_domain,
            "provider_instance": self.provider_instance,
            "media_types": [x.value for x in self.media_types],
        }
