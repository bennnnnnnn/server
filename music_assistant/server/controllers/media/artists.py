"""Manage MediaItems of type Artist."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
from random import choice, random
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError, UnsupportedFeaturedException
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ItemMapping,
    MediaType,
    PagedItems,
    Track,
)
from music_assistant.constants import VARIOUS_ARTISTS, VARIOUS_ARTISTS_ID
from music_assistant.server.controllers.media.base import MediaControllerBase
from music_assistant.server.controllers.music import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_TRACKS,
)
from music_assistant.server.helpers.compare import compare_strings

if TYPE_CHECKING:
    from music_assistant.server.models.music_provider import MusicProvider


class ArtistsController(MediaControllerBase[Artist]):
    """Controller managing MediaItems of type Artist."""

    db_table = DB_TABLE_ARTISTS
    media_type = MediaType.ARTIST
    item_cls = Artist

    def __init__(self, *args, **kwargs):
        """Initialize class."""
        super().__init__(*args, **kwargs)
        # register api handlers
        self.mass.register_api_command("music/artists", self.db_items)
        self.mass.register_api_command("music/albumartists", self.album_artists)
        self.mass.register_api_command("music/artist", self.get)
        self.mass.register_api_command("music/artist/albums", self.albums)
        self.mass.register_api_command("music/artist/tracks", self.tracks)
        self.mass.register_api_command("music/artist/update", self.update_db_item)
        self.mass.register_api_command("music/artist/delete", self.delete_db_item)

    async def album_artists(
        self,
        in_library: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
    ) -> PagedItems:
        """Get in-database album artists."""
        return await self.db_items(
            in_library=in_library,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            query_parts=["artists.sort_name in (select albums.sort_artist from albums)"],
        )

    async def tracks(
        self,
        item_id: str | None = None,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        artist: Artist | None = None,
    ) -> list[Track]:
        """Return top tracks for an artist."""
        if not artist:
            artist = await self.get(item_id, provider_domain, provider_instance, add_to_db=False)
        # get results from all providers
        coros = [
            self.get_provider_artist_toptracks(
                prov_mapping.item_id,
                provider_domain=prov_mapping.provider_domain,
                provider_instance=prov_mapping.provider_instance,
                cache_checksum=artist.metadata.checksum,
            )
            for prov_mapping in artist.provider_mappings
        ]
        tracks = itertools.chain.from_iterable(await asyncio.gather(*coros))
        # merge duplicates using a dict
        final_items: dict[str, Track] = {}
        for track in tracks:
            key = f".{track.name}.{track.version}"
            if key in final_items:
                final_items[key].provider_mappings.update(track.provider_mappings)
            else:
                final_items[key] = track
        return list(final_items.values())

    async def albums(
        self,
        item_id: str | None = None,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        artist: Artist | None = None,
    ) -> list[Album]:
        """Return (all/most popular) albums for an artist."""
        if not artist:
            artist = await self.get(item_id, provider_domain or provider_instance, add_to_db=False)
        # get results from all providers
        coros = [
            self.get_provider_artist_albums(
                item.item_id,
                item.provider_domain,
                cache_checksum=artist.metadata.checksum,
            )
            for item in artist.provider_mappings
        ]
        albums = itertools.chain.from_iterable(await asyncio.gather(*coros))
        # merge duplicates using a dict
        final_items: dict[str, Album] = {}
        for album in albums:
            key = f".{album.name}.{album.version}"
            if key in final_items:
                final_items[key].provider_mappings.update(album.provider_mappings)
            else:
                final_items[key] = album
            if album.in_library:
                final_items[key].in_library = True
        return list(final_items.values())

    async def add(self, item: Artist) -> Artist:
        """Add artist to local db and return the database item."""
        # grab musicbrainz id and additional metadata
        await self.mass.metadata.get_artist_metadata(item)
        existing = await self.get_db_item_by_prov_id(item.item_id, item.provider)
        if existing:
            db_item = await self.update_db_item(existing.item_id, item)
        else:
            db_item = await self.add_db_item(item)
        # also fetch same artist on all providers
        await self.match_artist(db_item)
        # return final db_item after all match/metadata actions
        db_item = await self.get_db_item(db_item.item_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED if existing else EventType.MEDIA_ITEM_ADDED,
            db_item.uri,
            db_item,
        )
        return db_item

    async def match_artist(self, db_artist: Artist):
        """Try to find matching artists on all providers for the provided (database) item_id.

        This is used to link objects of different providers together.
        """
        assert db_artist.provider == "database", "Matching only supported for database items!"
        cur_provider_domains = {x.provider_domain for x in db_artist.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if await self._match(db_artist, provider):
                cur_provider_domains.add(provider.domain)
            else:
                self.logger.debug(
                    "Could not find match for Artist %s on provider %s",
                    db_artist.name,
                    provider.name,
                )

    async def get_provider_artist_toptracks(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        cache_checksum: Any = None,
    ) -> list[Track]:
        """Return top tracks for an artist on given provider."""
        prov = self.mass.get_provider(provider_instance or provider_domain)
        if prov is None:
            return []
        # prefer cache items (if any)
        cache_key = f"{prov.instance_id}.artist_toptracks.{item_id}"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            return [Track.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        if ProviderFeature.ARTIST_TOPTRACKS in prov.supported_features:
            items = await prov.get_artist_toptracks(item_id)
        else:
            # fallback implementation using the db
            items = []
            if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(
                item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
            ):
                prov_id = provider_instance or provider_domain
                # TODO: adjust to json query instead of text search?
                query = f"SELECT * FROM tracks WHERE artists LIKE '%\"{db_artist.item_id}\"%'"
                query += f" AND provider_mappings LIKE '%\"{prov_id}\"%'"
                items = await self.mass.music.tracks.get_db_items_by_query(query)
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(cache_key, [x.to_dict() for x in items], checksum=cache_checksum)
        )
        return items

    async def get_provider_artist_albums(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        cache_checksum: Any = None,
    ) -> list[Album]:
        """Return albums for an artist on given provider."""
        prov = self.mass.get_provider(provider_instance or provider_domain)
        if prov is None:
            return []
        # prefer cache items (if any)
        cache_key = f"{prov.instance_id}.artist_albums.{item_id}"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            return [Album.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        if ProviderFeature.ARTIST_ALBUMS in prov.supported_features:
            items = await prov.get_artist_albums(item_id)
        else:
            # fallback implementation using the db
            if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(  # noqa: PLR5501
                item_id,
                provider_domain=provider_domain,
                provider_instance=provider_instance,
            ):
                prov_id = provider_instance or provider_domain
                # TODO: adjust to json query instead of text search?
                query = f"SELECT * FROM albums WHERE artists LIKE '%\"{db_artist.item_id}\"%'"
                query += f" AND provider_mappings LIKE '%\"{prov_id}\"%'"
                items = await self.mass.music.albums.get_db_items_by_query(query)
            else:
                # edge case
                items = []
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(cache_key, [x.to_dict() for x in items], checksum=cache_checksum)
        )
        return items

    async def add_db_item(self, item: Artist) -> Artist:
        """Add a new item record to the database."""
        assert isinstance(item, Artist), "Not a full Artist object"
        assert item.provider_mappings, "Item is missing provider mapping(s)"
        # enforce various artists name + id
        if compare_strings(item.name, VARIOUS_ARTISTS):
            item.musicbrainz_id = VARIOUS_ARTISTS_ID
        if item.musicbrainz_id == VARIOUS_ARTISTS_ID:
            item.name = VARIOUS_ARTISTS

        async with self._db_add_lock:
            # always try to grab existing item by musicbrainz_id
            cur_item = None
            if item.musicbrainz_id:
                match = {"musicbrainz_id": item.musicbrainz_id}
                cur_item = await self.mass.music.database.get_row(self.db_table, match)
            if not cur_item:
                # fallback to exact name match
                # NOTE: we match an artist by name which could theoretically lead to collisions
                # but the chance is so small it is not worth the additional overhead of grabbing
                # the musicbrainz id upfront
                match = {"sort_name": item.sort_name}
                for row in await self.mass.music.database.get_rows(self.db_table, match):
                    row_artist = Artist.from_db_row(row)
                    if row_artist.sort_name == item.sort_name:
                        cur_item = row_artist
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, item)

            # insert item
            item.timestamp_added = int(utc_timestamp())
            item.timestamp_modified = int(utc_timestamp())
            new_item = await self.mass.music.database.insert(self.db_table, item.to_db_row())
            item_id = new_item["item_id"]
            # update/set provider_mappings table
            await self._set_provider_mappings(item_id, item.provider_mappings)
            self.logger.debug("added %s to database", item.name)
            # return created object
            return await self.get_db_item(item_id)

    async def update_db_item(
        self,
        item_id: int,
        item: Artist,
    ) -> Artist:
        """Update Artist record in the database."""
        assert item.provider_mappings, "Item is missing provider mapping(s)"
        cur_item = await self.get_db_item(item_id)
        is_file_provider = item.provider.startswith("filesystem")
        metadata = cur_item.metadata.update(item.metadata, is_file_provider)
        provider_mappings = {*cur_item.provider_mappings, *item.provider_mappings}

        # enforce various artists name + id
        if compare_strings(item.name, VARIOUS_ARTISTS):
            item.musicbrainz_id = VARIOUS_ARTISTS_ID
        if item.musicbrainz_id == VARIOUS_ARTISTS_ID:
            item.name = VARIOUS_ARTISTS

        await self.mass.music.database.update(
            self.db_table,
            {"item_id": item_id},
            {
                "name": item.name if is_file_provider else cur_item.name,
                "sort_name": item.sort_name if is_file_provider else cur_item.sort_name,
                "musicbrainz_id": item.musicbrainz_id or cur_item.musicbrainz_id,
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(item_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        return await self.get_db_item(item_id)

    async def delete_db_item(self, item_id: int, recursive: bool = False) -> None:
        """Delete record from the database."""
        # check artist albums
        db_rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_ALBUMS} WHERE artists LIKE '%\"{item_id}\"%'",
            limit=5000,
        )
        assert not (db_rows and not recursive), "Albums attached to artist"
        for db_row in db_rows:
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.albums.delete_db_item(db_row["item_id"], recursive)

        # check artist tracks
        db_rows = await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_TRACKS} WHERE artists LIKE '%\"{item_id}\"%'",
            limit=5000,
        )
        assert not (db_rows and not recursive), "Tracks attached to artist"
        for db_row in db_rows:
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.albums.delete_db_item(db_row["item_id"], recursive)

        # delete the artist itself from db
        await super().delete_db_item(item_id)

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the artist's top tracks."""
        prov = self.mass.get_provider(provider_instance or provider_domain)
        if prov is None:
            return []
        if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
            return []
        top_tracks = await self.get_provider_artist_toptracks(
            item_id=item_id,
            provider_domain=provider_domain,
            provider_instance=provider_instance,
        )
        # Grab a random track from the album that we use to obtain similar tracks for
        track = choice(top_tracks)
        # Calculate no of songs to grab from each list at a 10/90 ratio
        total_no_of_tracks = limit + limit % 2
        no_of_artist_tracks = int(total_no_of_tracks * 10 / 100)
        no_of_similar_tracks = int(total_no_of_tracks * 90 / 100)
        # Grab similar tracks from the music provider
        similar_tracks = await prov.get_similar_tracks(
            prov_track_id=track.item_id, limit=no_of_similar_tracks
        )
        # Merge album content with similar tracks
        dynamic_playlist = [
            *sorted(top_tracks, key=lambda n: random())[:no_of_artist_tracks],  # noqa: ARG005
            *sorted(similar_tracks, key=lambda n: random())[:no_of_similar_tracks],  # noqa: ARG005
        ]
        return sorted(dynamic_playlist, key=lambda n: random())  # noqa: ARG005

    async def _get_dynamic_tracks(
        self, media_item: Artist, limit: int = 25  # noqa: ARG002
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        raise UnsupportedFeaturedException(
            "No Music Provider found that supports requesting similar tracks."
        )

    async def _match(self, db_artist: Artist, provider: MusicProvider) -> bool:
        """Try to find matching artists on given provider for the provided (database) artist."""
        self.logger.debug("Trying to match artist %s on provider %s", db_artist.name, provider.name)
        # try to get a match with some reference tracks of this artist
        for ref_track in await self.tracks(db_artist.item_id, db_artist.provider, artist=db_artist):
            # make sure we have a full track
            if isinstance(ref_track.album, ItemMapping):
                ref_track = await self.mass.music.tracks.get(  # noqa: PLW2901
                    ref_track.item_id, ref_track.provider, add_to_db=False
                )
            for search_str in (
                f"{db_artist.name} - {ref_track.name}",
                f"{db_artist.name} {ref_track.name}",
                ref_track.name,
            ):
                search_results = await self.mass.music.tracks.search(search_str, provider.domain)
                for search_result_item in search_results:
                    if search_result_item.sort_name != ref_track.sort_name:
                        continue
                    # get matching artist from track
                    for search_item_artist in search_result_item.artists:
                        if search_item_artist.sort_name != db_artist.sort_name:
                            continue
                        # 100% album match
                        # get full artist details so we have all metadata
                        prov_artist = await self.get_provider_item(
                            search_item_artist.item_id, search_item_artist.provider
                        )
                        await self.update_db_item(db_artist.item_id, prov_artist)
                        return True
        # try to get a match with some reference albums of this artist
        artist_albums = await self.albums(db_artist.item_id, db_artist.provider, artist=db_artist)
        for ref_album in artist_albums:
            if ref_album.album_type == AlbumType.COMPILATION:
                continue
            if ref_album.artist is None:
                continue
            for search_str in (
                ref_album.name,
                f"{db_artist.name} - {ref_album.name}",
                f"{db_artist.name} {ref_album.name}",
            ):
                search_result = await self.mass.music.albums.search(search_str, provider.domain)
                for search_result_item in search_result:
                    if search_result_item.artist is None:
                        continue
                    if search_result_item.sort_name != ref_album.sort_name:
                        continue
                    # artist must match 100%
                    if search_result_item.artist.sort_name != ref_album.artist.sort_name:
                        continue
                    # 100% match
                    # get full artist details so we have all metadata
                    prov_artist = await self.get_provider_item(
                        search_result_item.artist.item_id,
                        search_result_item.artist.provider,
                    )
                    await self.update_db_item(db_artist.item_id, prov_artist)
                    return True
        return False
