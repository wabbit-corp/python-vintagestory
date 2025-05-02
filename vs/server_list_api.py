#!/usr/bin/env python3
import asyncio
import logging
import re
import json
from dataclasses import dataclass, field, asdict
from typing import List, Tuple, Optional

import aiohttp
from bs4 import BeautifulSoup

# -------------------------------
# Data models
# -------------------------------

@dataclass
class GlobalStats:
    players_on_public_servers: int
    public_servers: int
    percent_v1_20: float
    players_over_time: List[Tuple[int, int]] = field(default_factory=list)

@dataclass
class ServerEntry:
    player_count: int
    join_link: Optional[str] = None
    is_whitelisted: bool = False
    is_password_protected: bool = False
    name: str = ""
    mod_count: Optional[int] = None
    description_html: Optional[str] = None
    discord_links: List[str] = field(default_factory=list)

# -------------------------------
# Fetching and Parsing functions
# -------------------------------

async def fetch_page(session: aiohttp.ClientSession, url: str) -> str:
    logging.info("Fetching URL: %s", url)
    async with session.get(url) as resp:
        resp.raise_for_status()
        text = await resp.text()
        logging.info("Fetched %d characters", len(text))
        return text

def parse_global_stats(soup: BeautifulSoup) -> GlobalStats:
    # The global stats appear in three boxes.
    boxes = soup.find_all("div", class_="box")
    if len(boxes) < 3:
        raise ValueError("Not enough boxes found for global stats")

    # First box: players on public servers
    players_on_public = int(boxes[0].find("span", class_="num").text.strip())
    # Second box: public servers
    public_servers = int(boxes[1].find("span", class_="num").text.strip())
    # Third box: percent on v1.20 (remove the % symbol)
    percent_text = boxes[2].find("span", class_="num").text.strip()
    percent_v1_20 = float(percent_text.rstrip('%'))

    # Extract players over time data from the embedded JavaScript
    players_over_time = []
    # Look for a script tag that contains "var data ="
    for script in soup.find_all("script", type="text/javascript"):
        if script.string and "var data =" in script.string:
            # Use regex to extract the JavaScript array (the data is an array of arrays)
            match = re.search(r"var\s+data\s*=\s*(\[[\s\S]*?\]);", script.string)
            if match:
                data_str = match.group(1)
                try:
                    # The JS array is almost valid JSON. (Numbers and arrays only)
                    players_over_time = json.loads(data_str)
                    logging.info("Parsed players over time: %d data points", len(players_over_time))
                except Exception as e:
                    logging.error("Error parsing players over time data: %s", e)
            break

    return GlobalStats(
        players_on_public_servers=players_on_public,
        public_servers=public_servers,
        percent_v1_20=percent_v1_20,
        players_over_time=players_over_time,
    )

def parse_server_list(soup: BeautifulSoup) -> List[ServerEntry]:
    server_entries = []
    server_list_div = soup.find("div", class_="serverlist")
    if not server_list_div:
        logging.warning("No server list found")
        return server_entries

    # Each server entry is in a div with class "server"
    servers = server_list_div.find_all("div", class_="server")
    for server in servers:
        try:
            # --- Player count ---
            b_tag = server.find("b")
            player_text = b_tag.get_text(strip=True) if b_tag else ""
            m = re.search(r"(\d+)", player_text)
            player_count = int(m.group(1)) if m else 0

            # --- Name, join link, and protection flags ---
            join_link = None
            name = ""
            is_whitelisted = False
            is_password_protected = False

            # Check if there is an <a> tag with a vintagestoryjoin:// link
            a_tag = server.find("a", href=re.compile(r"^vintagestoryjoin://"))
            if a_tag:
                join_link = a_tag["href"]
                name = a_tag.get_text(strip=True)
            else:
                # If no join link is present, try to find an <abbr> element
                abbr_tag = server.find("abbr")
                if abbr_tag:
                    name = abbr_tag.get_text(strip=True)
                    title_attr = abbr_tag.get("title", "")
                    if "Whitelisted" in title_attr:
                        is_whitelisted = True
                    if "Password protected" in title_attr:
                        is_password_protected = True
                else:
                    # As fallback, use text after the "on" keyword
                    text = server.get_text(" ", strip=True)
                    parts = text.split("on", 1)
                    if len(parts) > 1:
                        name = parts[1].strip()

            # --- Mod count ---
            mod_count = None
            img_tag = server.find("img", title=re.compile(r"\d+\s+mods installed"))
            if img_tag:
                title_text = img_tag.get("title", "")
                m_mod = re.search(r"(\d+)\s+mods installed", title_text)
                if m_mod:
                    mod_count = int(m_mod.group(1))

            # --- Description HTML ---
            desc_div = server.find("div", class_="serverdesc")
            description_html = str(desc_div) if desc_div else ""

            # --- Extract any discord links from the description ---
            discord_links = []
            if description_html:
                discord_links = re.findall(r"https?://discord\.gg/[^\s'\"<>]+", description_html)

            entry = ServerEntry(
                player_count=player_count,
                join_link=join_link,
                is_whitelisted=is_whitelisted,
                is_password_protected=is_password_protected,
                name=name,
                mod_count=mod_count,
                description_html=description_html,
                discord_links=discord_links,
            )
            server_entries.append(entry)
        except Exception as e:
            logging.error("Error parsing a server entry: %s", e)
    logging.info("Parsed %d server entries", len(server_entries))
    return server_entries

async def parse_servers_page(url: str) -> Tuple[GlobalStats, List[ServerEntry]]:
    async with aiohttp.ClientSession() as session:
        html = await fetch_page(session, url)
        soup = BeautifulSoup(html, "html.parser")
        global_stats = parse_global_stats(soup)
        servers = parse_server_list(soup)
        return global_stats, servers


# -------------------------------
# Main entry point
# -------------------------------

def main():
    logging.basicConfig(level=logging.INFO)
    url = "https://servers.vintagestory.at/"
    global_stats, servers = asyncio.run(parse_servers_page(url))

    print("Global Stats:")
    print(asdict(global_stats))
    print()

    print("Server List:")
    for server in servers:
        print(asdict(server))
        print()

if __name__ == "__main__":
    main()
