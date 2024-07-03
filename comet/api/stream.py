import asyncio
import hashlib
import json
import time
import aiohttp

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from RTN import Torrent, parse, sort_torrents, title_match

from comet.utils.general import (bytes_to_size, config_check, check_info_hash,
                                 generate_download_link, get_indexer_manager,
                                 get_torrent_hash, is_video, translate, get_balanced_hashes)
from comet.utils.logger import logger
from comet.utils.models import database, rtn, settings

streams = APIRouter()

@streams.get("/stream/{type}/{id}.json")
@streams.get("/{b64config}/stream/{type}/{id}.json")
async def stream(request: Request, b64config: str, type: str, id: str):
    config = config_check(b64config)
    if not config:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet", 
                        "title": "Invalid Comet config.",
                        "url": "https://comet.fast"
                    }
                ]
            }
    
    async with aiohttp.ClientSession() as session:
        check_debrid = await session.get("https://api.real-debrid.com/rest/1.0/user", headers={
            "Authorization": f"Bearer {config['debridApiKey']}"
        })
        check_debrid = await check_debrid.text()
        if not '"type": "premium"' in check_debrid:
            return {
                "streams": [
                    {
                        "name": "[⚠️] Comet", 
                        "title": "Invalid Real-Debrid account.",
                        "url": "https://comet.fast"
                    }
                ]
            }

        season = None
        episode = None
        if type == "series":
            info = id.split(":")

            id = info[0]
            season = int(info[1])
            episode = int(info[2])

        get_metadata = await session.get(f"https://v3.sg.media-imdb.com/suggestion/a/{id}.json")
        metadata = await get_metadata.json()

        name = metadata["d"][0]["l"]
        name = translate(name)

        cache_key = hashlib.md5(json.dumps({"debridService": config["debridService"], "name": name, "season": season, "episode": episode, "indexers": config["indexers"]}).encode("utf-8")).hexdigest()
        cached = await database.fetch_one(f"SELECT EXISTS (SELECT 1 FROM cache WHERE cacheKey = '{cache_key}')")
        if cached[0] != 0:
            logger.info(f"Cache found for {name}")

            timestamp = await database.fetch_one(f"SELECT timestamp FROM cache WHERE cacheKey = '{cache_key}'")
            if timestamp[0] + settings.CACHE_TTL < time.time():
                await database.execute(f"DELETE FROM cache WHERE cacheKey = '{cache_key}'")

                logger.info(f"Cache expired for {name}")
            else:
                sorted_ranked_files = await database.fetch_one(f"SELECT results FROM cache WHERE cacheKey = '{cache_key}'")
                sorted_ranked_files = json.loads(sorted_ranked_files[0])
                
                balanced_hashes = await get_balanced_hashes(sorted_ranked_files, config)
                
                results = []
                for hash in sorted_ranked_files:
                    for resolution in balanced_hashes:
                        if hash in balanced_hashes[resolution]:
                            results.append({
                                "name": f"[RD⚡] Comet {sorted_ranked_files[hash]['data']['resolution'][0] if len(sorted_ranked_files[hash]['data']['resolution']) > 0 else 'Unknown'}",
                                "title": f"{sorted_ranked_files[hash]['data']['title']}\n💾 {bytes_to_size(sorted_ranked_files[hash]['data']['size'])}",
                                "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sorted_ranked_files[hash]['data']['index']}"
                            })

                            continue

                return {
                    "streams": results
                }
        else:
            logger.info(f"No cache found for {name} with user configuration")

        indexer_manager_type = settings.INDEXER_MANAGER_TYPE

        logger.info(f"Start of {indexer_manager_type} search for {name} with indexers {config['indexers']}")

        tasks = []
        tasks.append(get_indexer_manager(session, indexer_manager_type, config["indexers"], name))
        if type == "series":
            tasks.append(get_indexer_manager(session, indexer_manager_type, config["indexers"], f"{name} S0{season}E0{episode}"))
        search_response = await asyncio.gather(*tasks)

        torrents = []
        for results in search_response:
            if results == None:
                continue

            for result in results:
                torrents.append(result)

        logger.info(f"{len(torrents)} torrents found for {name} with {indexer_manager_type}")

        zilean_hashes_count = 0
        try:
            if settings.ZILEAN_URL:
                get_dmm = await session.post(f"{settings.ZILEAN_URL}/dmm/search", json={
                    "queryText": name
                })
                get_dmm = await get_dmm.json()

                if not "status" in get_dmm:
                    for result in get_dmm:
                        zilean_hashes_count += 1

                        if indexer_manager_type == "jackett":
                            object = {
                                "Title": result["filename"],
                                "InfoHash": result["infoHash"],
                                "zilean": True
                            }

                        if indexer_manager_type == "prowlarr":
                            object = {
                                "title": result["filename"],
                                "infoHash": result["infoHash"],
                                "zilean": True
                            }

                        torrents.append(object)

            logger.info(f"{zilean_hashes_count} torrents found for {name} with Zilean API")
        except:
            logger.warning(f"Exception while getting torrents for {name} with Zilean API")

        if len(torrents) == 0:
            return {"streams": []}

        tasks = []
        filtered = 0
        for torrent in torrents:
            # Only title match check if from Zilean
            if "zilean" in torrent:
                parsed_torrent = parse(torrent["Title"] if indexer_manager_type == "jackett" else torrent["title"])
                if not title_match(name.lower(), parsed_torrent.parsed_title.lower()):
                    filtered += 1
                    continue

            tasks.append(get_torrent_hash(session, indexer_manager_type, torrent))

        logger.info(f"{filtered} filtered torrents from Zilean API for {name}")
    
        torrent_hashes = await asyncio.gather(*tasks)
        torrent_hashes = list(set([hash for hash in torrent_hashes if hash]))

        logger.info(f"{len(torrent_hashes)} info hashes found for {name}")

        torrent_hashes = list(set([hash for hash in torrent_hashes if hash]))
        
        if len(torrent_hashes) == 0:
            return {"streams": []}

        tasks = []
        for hash in torrent_hashes:
            tasks.append(check_info_hash(config["debridApiKey"], hash))

        responses = await asyncio.gather(*tasks)

        availability = {}
        for response in responses:
            if not response:
                continue
            
            availability.update(await response.json())

        files = {}
        for hash, details in availability.items():
            if not "rd" in details:
                continue

            if type == "series":
                for variants in details["rd"]:
                    for index, file in variants.items():
                        filename = file["filename"]
                        
                        if not is_video(filename):
                            continue

                        filename_parsed = parse(filename)
                        if season in filename_parsed.season and episode in filename_parsed.episode:
                            files[hash] = {
                                "index": index,
                                "title": filename,
                                "size": file["filesize"]
                            }

                continue

            for variants in details["rd"]:
                for index, file in variants.items():
                    filename = file["filename"]

                    if not is_video(filename):
                        continue

                    files[hash] = {
                        "index": index,
                        "title": filename,
                        "size": file["filesize"]
                    }

        ranked_files = set()
        for hash in files:
            ranked_file = rtn.rank(files[hash]["title"], hash)
            ranked_files.add(ranked_file)
        
        sorted_ranked_files = sort_torrents(ranked_files)

        logger.info(f"{len(sorted_ranked_files)} cached files found on Real-Debrid for {name}")

        if len(sorted_ranked_files) == 0:
            return {"streams": []}
        
        sorted_ranked_files = {
            key: (value.model_dump() if isinstance(value, Torrent) else value)
            for key, value in sorted_ranked_files.items()
        }
        for hash in sorted_ranked_files: # needed for caching
            sorted_ranked_files[hash]["data"]["title"] = files[hash]["title"]
            sorted_ranked_files[hash]["data"]["size"] = files[hash]["size"]
            sorted_ranked_files[hash]["data"]["index"] = files[hash]["index"]
        
        json_data = json.dumps(sorted_ranked_files).replace("'", "''")
        await database.execute(f"INSERT OR IGNORE INTO cache (cacheKey, results, timestamp) VALUES ('{cache_key}', '{json_data}', {time.time()})")
        logger.info(f"Results have been cached for {name}")

        balanced_hashes = await get_balanced_hashes(sorted_ranked_files, config)
        
        results = []
        for hash in sorted_ranked_files:
            for resolution in balanced_hashes:
                if hash in balanced_hashes[resolution]:
                    results.append({
                        "name": f"[RD⚡] Comet {sorted_ranked_files[hash]['data']['resolution'][0] if len(sorted_ranked_files[hash]['data']['resolution']) > 0 else 'Unknown'}",
                        "title": f"{sorted_ranked_files[hash]['data']['title']}\n💾 {bytes_to_size(sorted_ranked_files[hash]['data']['size'])}",
                        "url": f"{request.url.scheme}://{request.url.netloc}/{b64config}/playback/{hash}/{sorted_ranked_files[hash]['data']['index']}"
                    }) 

                    continue

        return {
            "streams": results
        }

@streams.head("/{b64config}/playback/{hash}/{index}")
async def playback(b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return

    download_link = await generate_download_link(config["debridApiKey"], hash, index)

    return RedirectResponse(download_link, status_code=302)

@streams.get("/{b64config}/playback/{hash}/{index}")
async def playback(b64config: str, hash: str, index: str):
    config = config_check(b64config)
    if not config:
        return

    download_link = await generate_download_link(config["debridApiKey"], hash, index)

    return RedirectResponse(download_link, status_code=302)