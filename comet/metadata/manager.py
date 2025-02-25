import aiohttp
import asyncio
import time
import orjson

from RTN.patterns import normalize_title

from comet.utils.models import database, settings
from comet.utils.general import parse_media_id

from .kitsu import get_kitsu_metadata, get_kitsu_aliases
from .imdb import get_imdb_metadata
from .trakt import get_trakt_aliases


class MetadataScraper:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_metadata_and_aliases(self, media_type: str, media_id: str):
        id, season, episode = parse_media_id(media_type, media_id)

        real_id = id if id != "kitsu" else str(season)

        get_cached = await self.get_cached(
            real_id, season if id != "kitsu" else 1, episode
        )
        if get_cached is not None:
            return get_cached[0], get_cached[1]

        metadata_task = asyncio.create_task(self.get_metadata(id, season, episode))
        aliases_task = asyncio.create_task(self.get_aliases(media_type, id, season))
        metadata, aliases = await asyncio.gather(metadata_task, aliases_task)
        await self.cache_metadata(real_id, metadata, aliases)

        return metadata, aliases

    async def get_cached(self, media_id: str, season: int, episode: int):
        row = await database.fetch_one(
            """
                SELECT title, year, year_end, aliases
                FROM metadata_cache
                WHERE media_id = :media_id
                AND timestamp + :cache_ttl >= :current_time
            """,
            {
                "media_id": media_id,
                "cache_ttl": settings.METADATA_CACHE_TTL,
                "current_time": time.time(),
            },
        )
        if row is not None:
            metadata = {
                "title": row["title"],
                "year": row["year"],
                "year_end": row["year_end"],
                "season": season,
                "episode": episode,
            }
            return metadata, orjson.loads(row["aliases"])

        return None

    async def cache_metadata(self, media_id: str, metadata: dict, aliases: dict):
        await database.execute(
            f"""
                INSERT {"OR IGNORE " if settings.DATABASE_TYPE == "sqlite" else ""}
                INTO metadata_cache
                VALUES (:media_id, :title, :year, :year_end, :aliases, :timestamp)
                {" ON CONFLICT DO NOTHING" if settings.DATABASE_TYPE == "postgresql" else ""}
            """,
            {
                "media_id": media_id,
                "title": metadata["title"],
                "year": metadata["year"],
                "year_end": metadata["year_end"],
                "aliases": orjson.dumps(aliases).decode("utf-8"),
                "timestamp": time.time(),
            },
        )

    def normalize_metadata(self, metadata: dict, season: int, episode: int):
        title, year, year_end = metadata

        if title is None:  # metadata retrieving failed
            return None

        return {
            "title": normalize_title(title),
            "year": year,
            "year_end": year_end,
            "season": season,
            "episode": episode,
        }

    async def get_metadata(self, id: str, season: int, episode: int):
        if id == "kitsu":
            raw_metadata = await get_kitsu_metadata(self.session, season)
            return self.normalize_metadata(raw_metadata, 1, episode)
        else:
            raw_metadata = await get_imdb_metadata(self.session, id)
            return self.normalize_metadata(raw_metadata, season, episode)

    async def get_aliases(self, media_type: str, media_id: str, kitsu_id: str):
        if media_id == "kitsu":
            return await get_kitsu_aliases(self.session, kitsu_id)

        return await get_trakt_aliases(self.session, media_type, media_id)
