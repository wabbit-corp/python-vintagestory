import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Union
import re

import aiohttp
from aiohttp import ClientSession
from aiohttp.client_exceptions import ClientError
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------------
# Constants and Endpoints
# --------------------------------------------------------------------------------

BASE_URL = "https://mods.vintagestory.at"
API_BASE_URL = f"{BASE_URL}/api"

ALL_MODS_PAGE_URL = f"{BASE_URL}/list/mod"
MOD_PAGE_URL_TEMPLATE_ASSET = f"{BASE_URL}/show/mod/{{}}"
MOD_PAGE_URL_TEMPLATE_SLUG = f"{BASE_URL}/{{}}"

# Alternatively, if the user is known by its slug:
#   e.g. https://mods.vintagestory.at/artofcooking
# Some mods can also be accessed by direct slug path (like "/artofcooking"),
# but the HTML structure is identical.


# --------------------------------------------------------------------------------
# Rate Limiting
# --------------------------------------------------------------------------------

class SimpleRateLimiter:
    """
    A naive rate limiter that allows up to `max_calls` in `period` seconds,
    implemented as a rolling token bucket.
    """
    def __init__(self, max_calls: int = 1, period: float = 1.0):
        """
        :param max_calls: The maximum number of calls in `period` seconds.
        :param period: The time window in seconds during which `max_calls` calls are allowed.
        """
        self.max_calls = max_calls
        self.period = period
        self.allowance = max_calls
        self.last_check = time.time()

    async def acquire(self):
        """
        Acquire permission to proceed with one request.
        """
        while True:
            current_time = time.time()
            time_passed = current_time - self.last_check
            self.last_check = current_time
            self.allowance += time_passed * (self.max_calls / self.period)
            if self.allowance > self.max_calls:
                self.allowance = self.max_calls

            if self.allowance < 1.0:
                # Not enough allowance, must wait
                await asyncio.sleep(0.1)
            else:
                self.allowance -= 1.0
                return


# --------------------------------------------------------------------------------
# Data Models
# --------------------------------------------------------------------------------

@dataclass
class ModSearchFilterOptions:
    """
    Represents the list of filter options discovered by scraping
    the /list/mod page.

    Each field is a dict or list of (value -> label) or similar structures.
    """
    sides: Dict[str, str]                # e.g. {"": "Any", "both": "Both", "client": "Client side mod", ...}
    tags: Dict[str, str]                 # tag_id -> tag_name
    authors: Dict[str, str]             # user_id -> author_name
    major_game_versions: Dict[str, str]  # "1": "1.15.x", "2": "1.14.x", ...
    exact_game_versions: Dict[str, str]  # "267": "v1.20.3", "266": "v1.20.2", ...
    # Additional fields could be added if needed (e.g. sort options).


@dataclass
class ModSearchResult:
    """
    Represents a single mod result from the /api/mods endpoint
    """
    modid: int
    assetid: int
    name: str
    summary: str
    author: str
    urlalias: str
    side: str
    type: str
    downloads: int
    follows: int
    trendingpoints: int
    comments: int
    logo: Optional[str]
    tags: List[str]
    modidstrs: List[str]
    lastreleased: Optional[str]


@dataclass
class ModRelease:
    """
    Represents a single release (file) for a given mod,
    from /api/mod/<modid> endpoint
    """
    releaseid: int
    modversion: str
    created: str
    fileid: Optional[int]
    filename: Optional[str]
    downloads: int
    mainfile: Optional[str]
    tags: List[str]


@dataclass
class ModScreenshot:
    fileid: int
    mainfile: str
    filename: str
    thumbnailfilename: Optional[str]
    created: str


@dataclass
class ModApiDetail:
    """
    The core data returned by the official /api/mod/<modid> or /api/mod/<slug> endpoint.

    Some fields are only partially used in this sample code (feel free to expand).
    """
    modid: int
    assetid: int
    name: str
    text: str
    author: str
    urlalias: str
    logofile: Optional[str]
    homepageurl: Optional[str]
    sourcecodeurl: Optional[str]
    trailervideourl: Optional[str]
    issuetrackerurl: Optional[str]
    wikiurl: Optional[str]
    downloads: int
    follows: int
    trendingpoints: int
    comments: int
    side: str
    type: str
    created: str
    lastmodified: str
    tags: List[str]
    releases: List[ModRelease] = field(default_factory=list)
    screenshots: List[ModScreenshot] = field(default_factory=list)


@dataclass
class ModHtmlExtras:
    """
    Data extracted from the mod's main HTML page, which is not included in the
    official JSON. For example, comments, 1-click links for each release, etc.
    """
    # Basic example fields:
    comments: List[str]                # or more structured data if you prefer
    # 1-click install links may differ from the JSON, so we can store them if needed:
    one_click_links: Dict[str, str]    # (version -> link) or (releaseid -> link)
    screenshot_urls: List[str]         # direct image links, or thumbnails, etc.


# --------------------------------------------------------------------------------
# The main client class
# --------------------------------------------------------------------------------

class VSModDBClient:
    """
    Asynchronous client to query mods.vintagestory.at using both:
      - The official JSON endpoints (faster, structured).
      - The HTML pages for extra fields (comments, 1-click install link, etc.)
    """

    def __init__(
        self,
        session: Optional[ClientSession] = None,
        max_calls_per_sec: float = 1.0
    ):
        """
        :param session: Optional externally-managed aiohttp session.
        :param max_calls_per_sec: Rate-limit to avoid spamming the site (1 call/sec by default).
        """
        self._session_external = session is not None
        self.session = session or aiohttp.ClientSession()
        self.ratelimiter = SimpleRateLimiter(max_calls=1, period=1.0 / max_calls_per_sec)

        self.logger = logging.getLogger(self.__class__.__name__)

    async def close(self):
        """Close the underlying session if we created it internally."""
        if not self._session_external and not self.session.closed:
            await self.session.close()

    # --------------------------------------------------------------------------
    # Internal helper
    # --------------------------------------------------------------------------
    async def _get_html(self, url: str) -> str:
        """GET an HTML page with rate-limiting and returning text."""
        await self.ratelimiter.acquire()
        self.logger.debug("Fetching HTML: %s", url)
        try:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                return await resp.text()
        except ClientError as e:
            self.logger.error("Failed to fetch %s: %s", url, e)
            raise

    async def _get_json(self, url: str, params: Dict[str, str] = None) -> Dict:
        """GET JSON from the mod DB API with rate-limiting, returning parsed data."""
        await self.ratelimiter.acquire()
        self.logger.debug("Fetching JSON: %s params=%s", url, params)
        try:
            async with self.session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()
        except ClientError as e:
            self.logger.error("Failed to fetch JSON %s: %s", url, e)
            raise

    # --------------------------------------------------------------------------
    # 1) Scrape filter options from the /list/mod HTML page
    # --------------------------------------------------------------------------
    async def fetch_search_filters(self) -> ModSearchFilterOptions:
        """
        Scrape the main All Mods page to discover:
         - Sides
         - Tags
         - Authors
         - Major game versions
         - Exact game versions

        Return them in structured form.
        """
        html = await self._get_html(ALL_MODS_PAGE_URL)
        soup = BeautifulSoup(html, "html.parser")

        # Sides <select name="side">
        side_sel = soup.find("select", {"name": "side"})
        sides = {}
        if side_sel:
            for opt in side_sel.find_all("option"):
                val = opt.get("value", "").strip()
                txt = opt.text.strip()
                sides[val] = txt

        # Tags <select name="tagids[]" multiple>
        tags_sel = soup.find("select", {"name": "tagids[]"})
        tags = {}
        if tags_sel:
            for opt in tags_sel.find_all("option"):
                val = opt.get("value", "").strip()
                txt = opt.text.strip()
                # You might also want the 'title' attribute
                # but for now we just store value->text
                if val:
                    tags[val] = txt

        # Authors <select name="userid">
        authors_sel = soup.find("select", {"name": "userid"})
        authors = {}
        if authors_sel:
            for opt in authors_sel.find_all("option"):
                val = opt.get("value", "").strip()
                txt = opt.text.strip()
                if val:
                    authors[val] = txt

        # Major game versions <select name="mv">
        mgv_sel = soup.find("select", {"name": "mv"})
        major_game_versions = {}
        if mgv_sel:
            for opt in mgv_sel.find_all("option"):
                val = opt.get("value", "").strip()
                txt = opt.text.strip()
                if val:
                    major_game_versions[val] = txt

        # Exact game versions <select name="gv[]" multiple>
        egv_sel = soup.find("select", {"name": "gv[]"})
        exact_game_versions = {}
        if egv_sel:
            for opt in egv_sel.find_all("option"):
                val = opt.get("value", "").strip()
                txt = opt.text.strip()
                if val:
                    exact_game_versions[val] = txt

        return ModSearchFilterOptions(
            sides=sides,
            tags=tags,
            authors=authors,
            major_game_versions=major_game_versions,
            exact_game_versions=exact_game_versions
        )

    # --------------------------------------------------------------------------
    # 2) Search for mods using the official /api/mods endpoint
    # --------------------------------------------------------------------------
    async def search_mods(
        self,
        text: Optional[str] = None,
        tagids: Optional[List[int]] = None,
        author: Optional[int] = None,
        gameversion: Optional[int] = None,
        gameversions: Optional[List[int]] = None,
        orderby: str = "asset.created",
        orderdirection: str = "desc"
    ) -> List[ModSearchResult]:
        """
        Call /api/mods with optional parameters, returning structured results.

        The official code from the PHP side indicates these GET params:
          - text
          - tagids[] (AND)
          - author
          - gameversion
          - gameversions[] (OR)
          - orderby ( one of: asset.created, lastreleased, downloads, follows, comments, trendingpoints )
          - orderdirection (desc or asc)
        """
        url = f"{API_BASE_URL}/mods"
        params: Dict[str, Union[str, List[str]]] = {}
        if text:
            params["text"] = text
        if tagids:
            for i, tid in enumerate(tagids):
                params[f"tagids[{i}]"] = str(tid)
        if author is not None:
            params["author"] = str(author)
        if gameversion is not None:
            params["gameversion"] = str(gameversion)
        if gameversions:
            for i, gv in enumerate(gameversions):
                params[f"gameversions[{i}]"] = str(gv)

        params["orderby"] = orderby
        params["orderdirection"] = orderdirection

        data = await self._get_json(url, params=params)
        if data.get("statuscode") != "200":
            self.logger.error("Non-200 status from search: %s", data)
            return []

        raw_mods = data.get("mods", [])
        results = []
        for m in raw_mods:
            results.append(ModSearchResult(
                modid=m["modid"],
                assetid=m["assetid"],
                name=m["name"],
                summary=m.get("summary", ""),
                author=m["author"],
                urlalias=m.get("urlalias", ""),
                side=m.get("side", ""),
                type=m.get("type", ""),
                downloads=m.get("downloads", 0),
                follows=m.get("follows", 0),
                trendingpoints=m.get("trendingpoints", 0),
                comments=m.get("comments", 0),
                logo=m.get("logo", None),
                tags=m.get("tags", []),
                modidstrs=m.get("modidstrs", []),
                lastreleased=m.get("lastreleased", None)
            ))
        return results
    
    async def get_mod_metadata(self, mod_identifier: Union[int, str]) -> Optional[ModApiDetail]:
        url = f"{API_BASE_URL}/mod/{mod_identifier}"
        data = await self._get_json(url)
        if data.get("statuscode") != "200":
            self.logger.warning("Mod not found or error. mod_identifier=%s data=%s", mod_identifier, data)
            return None

        raw_mod = data.get("mod", {})
        if not raw_mod:
            return None

        releases: List[ModRelease] = []
        for r in raw_mod.get("releases", []):
            releases.append(ModRelease(
                releaseid=r["releaseid"],
                modversion=r["modversion"],
                created=r["created"],
                fileid=r.get("fileid"),
                filename=r.get("filename"),
                downloads=r.get("downloads", 0),
                mainfile=r.get("mainfile"),
                tags=r.get("tags", [])
            ))

        screenshots: List[ModScreenshot] = []
        for sc in raw_mod.get("screenshots", []):
            screenshots.append(ModScreenshot(
                fileid=sc["fileid"],
                mainfile=sc["mainfile"],
                filename=sc["filename"],
                thumbnailfilename=sc["thumbnailfilename"],
                created=sc["created"]
            ))

        return ModApiDetail(
            modid=raw_mod["modid"],
            assetid=raw_mod["assetid"],
            name=raw_mod["name"],
            text=raw_mod["text"],
            author=raw_mod["author"],
            urlalias=raw_mod["urlalias"],
            logofile=raw_mod.get("logofile"),
            homepageurl=raw_mod.get("homepageurl"),
            sourcecodeurl=raw_mod.get("sourcecodeurl"),
            trailervideourl=raw_mod.get("trailervideourl"),
            issuetrackerurl=raw_mod.get("issuetrackerurl"),
            wikiurl=raw_mod.get("wikiurl"),
            downloads=raw_mod.get("downloads", 0),
            follows=raw_mod.get("follows", 0),
            trendingpoints=raw_mod.get("trendingpoints", 0),
            comments=raw_mod.get("comments", 0),
            side=raw_mod.get("side", ""),
            type=raw_mod.get("type", ""),
            created=raw_mod.get("created", ""),
            lastmodified=raw_mod.get("lastmodified", ""),
            tags=raw_mod.get("tags", []),
            releases=releases,
            screenshots=screenshots
        )

    # --------------------------------------------------------------------------
    # 3) Get a mod's metadata from /api/mod/<id-or-slug>
    # --------------------------------------------------------------------------
    async def get_mod_html_extras(self, mod_identifier: Union[int, str]) -> Optional[ModHtmlExtras]:
        # Determine URL as before.
        if isinstance(mod_identifier, int):
            meta = await self.get_mod_metadata(mod_identifier)
            if meta is None:
                self.logger.warning("Metadata lookup failed for modid %s", mod_identifier)
                return None
            target = str(meta.assetid)
            url = MOD_PAGE_URL_TEMPLATE_ASSET.format(target)
        elif isinstance(mod_identifier, str):
            if mod_identifier.isdigit():
                url = MOD_PAGE_URL_TEMPLATE_ASSET.format(mod_identifier)
            else:
                url = MOD_PAGE_URL_TEMPLATE_SLUG.format(mod_identifier)
        else:
            self.logger.error("Unsupported mod_identifier type: %s", type(mod_identifier))
            return None

        self.logger.debug("Fetching mod HTML extras from URL: %s", url)
        try:
            html = await self._get_html(url)
        except ClientError:
            self.logger.warning("Could not fetch mod page for %s at %s", mod_identifier, url)
            return None

        soup = BeautifulSoup(html, "html.parser")

        # Gather comments
        comment_divs = soup.select("div.comments div.comment")
        comments_text = []
        for cdiv in comment_divs:
            body_div = cdiv.select_one("div.body")
            if body_div:
                comments_text.append(body_div.get_text(strip=True))

        # Find the table with releases and extract the one-click install links.
        # Here we now use a regex to capture only the version number.
        one_click_map = {}
        file_table = soup.find("table", {"class": "stdtable"})
        if file_table:
            tbody = file_table.find("tbody")
            if tbody:
                rows = tbody.find_all("tr")
                version_pattern = re.compile(r"^(v\d+\.\d+\.\d+)")
                for r in rows:
                    version_td = r.find("td")
                    if not version_td:
                        continue
                    # Extract the text from the version cell.
                    full_text = version_td.get_text(strip=True)
                    # Use regex to extract just the version number.
                    m = version_pattern.search(full_text)
                    version_txt = m.group(1) if m else full_text
                    install_link_tag = r.find("a", href=lambda x: x and x.startswith("vintagestorymodinstall://"))
                    if install_link_tag:
                        href = install_link_tag["href"]
                        one_click_map[version_txt] = href

        # Screenshots extraction (unchanged)
        screenshot_urls = []
        slide_div = soup.select_one("div.imageslideshow.fotorama")
        if slide_div:
            for img_tag in slide_div.find_all("img"):
                src = img_tag.get("src")
                if src:
                    screenshot_urls.append(src)

        return ModHtmlExtras(
            comments=comments_text,
            one_click_links=one_click_map,
            screenshot_urls=screenshot_urls
        )

# --------------------------------------------------------------------------------
# Example usage
# --------------------------------------------------------------------------------

async def main():
    logging.basicConfig(level=logging.DEBUG)
    client = VSModDBClient(max_calls_per_sec=1.0)
    try:
        # 1) Discover search filters
        filters = await client.fetch_search_filters()
        print("Sides:", filters.sides)
        print("Tags: (showing first 10)", list(filters.tags.items())[:10])
        print("Authors: (showing first 10)", list(filters.authors.items())[:10])
        print("Major versions:", filters.major_game_versions)
        print("Exact versions: (showing first 10)", list(filters.exact_game_versions.items())[:10])

        # 2) Search for some mod by partial text
        search_results = await client.search_mods(text="Cooking", orderby="downloads")
        print(f"Found {len(search_results)} results searching for 'Cooking'")

        if search_results:
            # Just pick the first mod for demonstration
            modid = search_results[0].modid
            modslug = search_results[0].urlalias  # sometimes empty, but let's hope
            print("First result:", search_results[0])

            # 3) Detailed metadata from official JSON
            mod_meta = await client.get_mod_metadata(modid)
            if mod_meta:
                print("Official JSON mod name:", mod_meta.name)
                print("Release count:", len(mod_meta.releases))

            # 4) Additional data from HTML
            extras = await client.get_mod_html_extras(modid)
            if extras:
                print("Number of comments found in HTML:", len(extras.comments))
                print("One-click links discovered:", extras.one_click_links)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
