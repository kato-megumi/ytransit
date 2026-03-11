#!/usr/bin/env python3
"""
MCP Server for Yahoo! Transit Japan (transit.yahoo.co.jp).

Provides tools to search train/bus routes, get station info, and check
transit service status across Japan's rail and transit network.
"""

import json
import re
from enum import Enum
from typing import Any, Optional

import httpx
from bs4 import BeautifulSoup, Tag
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Initialize MCP server
mcp = FastMCP("yahoo_transit_mcp")

# Constants
BASE_URL = "https://transit.yahoo.co.jp"
SEARCH_URL = f"{BASE_URL}/search/result"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30.0


# ─── Enums ────────────────────────────────────────────────────────────

class SearchType(int, Enum):
    """Type of time specification for the search."""
    DEPARTURE = 1
    LAST_TRAIN = 2
    FIRST_TRAIN = 3
    ARRIVAL = 4


class TicketType(str, Enum):
    """Fare calculation type."""
    IC = "ic"
    NORMAL = "normal"


class SortOrder(str, Enum):
    """Sort order for results."""
    TIME = "time"
    TRANSFER = "transfer"
    PRICE = "price"


class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


# ─── HTTP Client ──────────────────────────────────────────────────────

async def _fetch_page(url: str, params: Optional[dict] = None) -> str:
    """Fetch a page from Yahoo Transit and return HTML content."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.9",
    }
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.text


def _handle_error(e: Exception) -> str:
    """Consistent error formatting."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 404:
            return "Error: Page not found. The station name may be incorrect."
        elif status == 403:
            return "Error: Access denied by Yahoo Transit."
        elif status == 429:
            return "Error: Rate limited. Please wait before retrying."
        return f"Error: HTTP {status} from Yahoo Transit."
    elif isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Please try again."
    return f"Error: {type(e).__name__}: {e}"


def _extract_lines_from_station_page(soup: BeautifulSoup, station_info: dict) -> None:
    """Extract rail line info from a station detail page.

    Lines appear in the '乗り入れ路線と時刻表' section as bold text with
    diainfo links (e.g., /diainfo/27/0) for service status.
    """
    # Strategy 1: Find diainfo links (運行情報) which sit next to line names
    for link in soup.find_all("a", href=re.compile(r"/diainfo/\d+")):
        parent = link.parent
        if parent:
            # The line name is typically a bold/strong sibling or the parent text
            bold = parent.find(["b", "strong"])
            if bold:
                line_name = _clean_text(bold.get_text())
            else:
                # Take all text before the link
                full_text = parent.get_text()
                line_name = _clean_text(full_text.split("運行情報")[0])
            if line_name and line_name not in station_info["lines"]:
                station_info["lines"].append(line_name)
                station_info["timetable_links"].append({
                    "line": line_name,
                    "service_info_url": BASE_URL + link["href"],
                })

    # Strategy 2: Fallback - parse from page text
    if not station_info["lines"]:
        text = soup.get_text()
        section_match = re.search(r"乗り入れ路線と時刻表(.+?)(?:駅設備|乗換検索)", text, re.DOTALL)
        if section_match:
            section = section_match.group(1)
            for line_match in re.finditer(
                r"(ＪＲ[^\n]+?|小田急[^\n]+?|東急[^\n]+?|京急[^\n]+?|"
                r"東京メトロ[^\n]+?|都営[^\n]+?|京王[^\n]+?|西武[^\n]+?|"
                r"東武[^\n]+?|相鉄[^\n]+?|江ノ島電鉄[^\n]*?|りんかい線[^\n]*?|"
                r"ゆりかもめ[^\n]*?|つくばエクスプレス[^\n]*?|横浜市営[^\n]*?)"
                r"(?=運行情報|時刻表|\s|$)",
                section,
            ):
                line_name = _clean_text(line_match.group(1))
                if line_name and line_name not in station_info["lines"]:
                    station_info["lines"].append(line_name)
                    station_info["timetable_links"].append({"line": line_name})


# ─── Parsers ──────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Clean up whitespace from scraped text."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_route_summary(route_div: Tag) -> dict:
    """Parse the summary header of a single route."""
    summary = {}

    # Route header text like "05:04発→06:03着59分（乗車52分）\n乗換：1回\nIC優先：990円\n53.1km"
    header = _clean_text(route_div.get_text())

    # Departure / arrival times
    time_match = re.search(r"(\d{1,2}:\d{2})発→(\d{1,2}:\d{2})着", header)
    if time_match:
        summary["departure_time"] = time_match.group(1)
        summary["arrival_time"] = time_match.group(2)

    # Total duration
    dur_match = re.search(r"着([\d時間]+分)", header)
    if dur_match:
        summary["duration"] = dur_match.group(1)

    # Ride time
    ride_match = re.search(r"乗車([\d時間]+分)", header)
    if ride_match:
        summary["ride_time"] = ride_match.group(1)

    # Transfers
    transfer_match = re.search(r"乗換[：:](\d+)回", header)
    if transfer_match:
        summary["transfers"] = int(transfer_match.group(1))

    # Fare
    fare_match = re.search(r"[：:]([\d,]+)円", header)
    if fare_match:
        summary["fare_yen"] = int(fare_match.group(1).replace(",", ""))

    # Distance
    dist_match = re.search(r"([\d.]+)km", header)
    if dist_match:
        summary["distance_km"] = float(dist_match.group(1))

    return summary


def _parse_route_legs(route_detail: Tag) -> list[dict]:
    """Parse individual legs (train segments) from a route detail section."""
    legs = []
    
    # Find all station list items
    stations = route_detail.find_all("li", class_=re.compile(r"(station|transit)"))
    if not stations:
        # Fallback: find station names from bold links
        stations = route_detail.find_all("div", class_=re.compile(r"station"))

    # Extract the textual content from the route detail and parse it structurally
    text = route_detail.get_text("\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    # Parse legs by finding train line info between stations
    current_leg: dict[str, Any] = {}

    for line in lines:
        # Station detection - matches station names (usually at departure/arrival)
        time_station = re.match(r"^(\d{1,2}:\d{2})\s*(発|着)$", line)
        if time_station:
            time_val = time_station.group(1)
            direction = time_station.group(2)
            if direction == "発":
                current_leg["departure_time"] = time_val
            elif direction == "着":
                if current_leg:
                    current_leg["arrival_time"] = time_val
            continue

        # Train line name detection
        train_match = re.match(r"^(ＪＲ.+|小田急.+|東急.+|京急.+|東京メトロ.+|都営.+|京王.+|西武.+|東武.+|相鉄.+|りんかい線.+|ゆりかもめ.+|つくばエクスプレス.+|横浜市営.+|.+線.+行)$", line)
        if train_match:
            if current_leg.get("line"):
                # Save previous leg
                legs.append(current_leg)
                current_leg = {}
            current_leg["line"] = _clean_text(train_match.group(1))
            continue

        # Platform info
        platform_match = re.search(r"\[発\]\s*(\S+)\s*→\s*\[着\]\s*(\S+)", line)
        if platform_match:
            current_leg["departure_platform"] = platform_match.group(1)
            current_leg["arrival_platform"] = platform_match.group(2)
            continue

        # Number of stops
        stops_match = re.match(r"^(\d+)駅$", line)
        if stops_match:
            current_leg["num_stops"] = int(stops_match.group(1))
            continue

        # Fare for this leg
        leg_fare = re.match(r"^([\d,]+)円$", line)
        if leg_fare:
            current_leg["fare_yen"] = int(leg_fare.group(1).replace(",", ""))
            continue

    # Append last leg
    if current_leg.get("line"):
        legs.append(current_leg)

    return legs


def _parse_station_names(route_detail: Tag) -> list[str]:
    """Extract station names from a route detail section."""
    stations = []
    # Filter out non-station links like 時刻表, 出口, 地図, 駅を登録
    exclude = {"時刻表", "出口", "地図", "駅を登録", "ルート保存", "定期券", "ルート共有", "印刷する"}
    for link in route_detail.find_all("a", href=re.compile(r"/station/\d+$")):
        name = _clean_text(link.get_text())
        if name and name not in stations and name not in exclude:
            stations.append(name)
    return stations


def _parse_routes(html: str) -> list[dict]:
    """Parse all routes from the Yahoo Transit search result page."""
    soup = BeautifulSoup(html, "lxml")
    routes = []

    # Find route sections - they use id="route01", "route02", etc.
    route_sections = soup.find_all("div", id=re.compile(r"^route\d+$"))

    if not route_sections:
        # Alternative: look for route summary divs
        route_sections = soup.find_all("div", class_=re.compile(r"routeDetail"))

    if not route_sections:
        # Last resort: parse from the full page text
        return _parse_routes_from_text(soup)

    for i, section in enumerate(route_sections, 1):
        route: dict[str, Any] = {"route_number": i}
        route["summary"] = _parse_route_summary(section)
        route["stations"] = _parse_station_names(section)
        route["legs"] = _parse_route_legs(section)
        routes.append(route)

    return routes


def _parse_routes_from_text(soup: BeautifulSoup) -> list[dict]:
    """Fallback parser: extract route info from full page text."""
    routes = []
    text = soup.get_text("\n", strip=True)

    # Split by route markers
    route_blocks = re.split(r"ルート(\d+)", text)

    i = 1
    while i < len(route_blocks) - 1:
        route_num = int(route_blocks[i])
        block = route_blocks[i + 1]

        route = {"route_number": route_num, "summary": {}, "stations": [], "legs": []}

        # Parse summary from block
        time_match = re.search(r"(\d{1,2}:\d{2})発→(\d{1,2}:\d{2})着", block)
        if time_match:
            route["summary"]["departure_time"] = time_match.group(1)
            route["summary"]["arrival_time"] = time_match.group(2)

        dur_match = re.search(r"着((?:\d+時間)?\d+分)", block)
        if dur_match:
            route["summary"]["duration"] = dur_match.group(1)

        ride_match = re.search(r"乗車((?:\d+時間)?\d+分)", block)
        if ride_match:
            route["summary"]["ride_time"] = ride_match.group(1)

        transfer_match = re.search(r"乗換[：:](\d+)回", block)
        if transfer_match:
            route["summary"]["transfers"] = int(transfer_match.group(1))

        fare_match = re.search(r"[：:]([\d,]+)円", block)
        if fare_match:
            route["summary"]["fare_yen"] = int(fare_match.group(1).replace(",", ""))

        dist_match = re.search(r"([\d.]+)km", block)
        if dist_match:
            route["summary"]["distance_km"] = float(dist_match.group(1))

        # Extract station names
        for station_match in re.finditer(r"[★●]?\s*([^\n]+?)\s*時刻表", block):
            name = _clean_text(station_match.group(1))
            if name:
                route["stations"].append(name)

        # Extract train lines
        for line_match in re.finditer(
            r"(ＪＲ[^\n]+行|小田急[^\n]+行|東急[^\n]+行|京急[^\n]+行|"
            r"東京メトロ[^\n]+行|都営[^\n]+行|京王[^\n]+行|西武[^\n]+行|"
            r"東武[^\n]+行|相鉄[^\n]+行|[^\n]+線[^\n]+行)",
            block,
        ):
            route["legs"].append({"line": _clean_text(line_match.group(1))})

        routes.append(route)
        i += 2

    return routes


# ─── Formatters ───────────────────────────────────────────────────────

def _format_routes_markdown(from_station: str, to_station: str, routes: list[dict]) -> str:
    """Format routes as Markdown."""
    if not routes:
        return f"No routes found from {from_station} to {to_station}."

    lines = [f"# {from_station} → {to_station}", ""]

    for route in routes:
        s = route.get("summary", {})
        lines.append(f"## Route {route['route_number']}")

        dep = s.get("departure_time", "?")
        arr = s.get("arrival_time", "?")
        duration = s.get("duration", "?")
        transfers = s.get("transfers", "?")
        fare = s.get("fare_yen", "?")
        distance = s.get("distance_km", "?")

        lines.append(f"- **Time**: {dep} → {arr} ({duration})")
        if s.get("ride_time"):
            lines.append(f"- **Ride time**: {s['ride_time']}")
        lines.append(f"- **Transfers**: {transfers}")
        lines.append(f"- **Fare**: ¥{fare:,}" if isinstance(fare, int) else f"- **Fare**: {fare}")
        lines.append(f"- **Distance**: {distance} km")

        # Stations
        if route.get("stations"):
            lines.append(f"- **Stations**: {' → '.join(route['stations'])}")

        # Legs
        if route.get("legs"):
            lines.append("")
            lines.append("### Details")
            for j, leg in enumerate(route["legs"], 1):
                line_name = leg.get("line", "Unknown line")
                lines.append(f"{j}. **{line_name}**")
                if leg.get("departure_time"):
                    lines.append(f"   - Depart: {leg['departure_time']}")
                if leg.get("arrival_time"):
                    lines.append(f"   - Arrive: {leg['arrival_time']}")
                if leg.get("departure_platform"):
                    lines.append(
                        f"   - Platform: {leg['departure_platform']} → {leg.get('arrival_platform', '?')}"
                    )
                if leg.get("num_stops"):
                    lines.append(f"   - Stops: {leg['num_stops']}")
                if leg.get("fare_yen"):
                    lines.append(f"   - Fare: ¥{leg['fare_yen']:,}")

        lines.append("")

    return "\n".join(lines)


def _format_routes_json(from_station: str, to_station: str, routes: list[dict]) -> str:
    """Format routes as JSON."""
    return json.dumps(
        {"from": from_station, "to": to_station, "routes": routes},
        ensure_ascii=False,
        indent=2,
    )


# ─── Input Models ─────────────────────────────────────────────────────

class TransitSearchInput(BaseModel):
    """Input for searching transit routes between two stations."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    origin: str = Field(
        ...,
        description="Departure station name in Japanese (e.g., '藤沢', '東京', '新宿'). English names like 'fujisawa' are also accepted and auto-converted.",
        min_length=1,
        max_length=100,
    )
    destination: str = Field(
        ...,
        description="Arrival station name in Japanese (e.g., '秋葉原', '渋谷', '横浜'). English names like 'akihabara' are also accepted and auto-converted.",
        min_length=1,
        max_length=100,
    )
    date: Optional[str] = Field(
        default=None,
        description="Travel date in YYYY-MM-DD format (e.g., '2026-03-12'). Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    time: Optional[str] = Field(
        default=None,
        description="Travel time in HH:MM 24h format (e.g., '08:30'). Defaults to current time.",
        pattern=r"^\d{2}:\d{2}$",
    )
    search_type: SearchType = Field(
        default=SearchType.DEPARTURE,
        description="Time type: 1=departure time, 2=last train, 3=first train, 4=arrival time",
    )
    ticket_type: TicketType = Field(
        default=TicketType.IC,
        description="Fare type: 'ic' for IC card fare, 'normal' for regular ticket fare",
    )
    sort: SortOrder = Field(
        default=SortOrder.TIME,
        description="Sort order: 'time'=fastest, 'transfer'=fewest transfers, 'price'=cheapest",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for readable text, 'json' for structured data",
    )

    @field_validator("origin", "destination")
    @classmethod
    def convert_romaji(cls, v: str) -> str:
        """Convert common romaji station names to Japanese."""
        romaji_map = {
            "tokyo": "東京",
            "shinjuku": "新宿",
            "shibuya": "渋谷",
            "ikebukuro": "池袋",
            "ueno": "上野",
            "akihabara": "秋葉原",
            "shinagawa": "品川",
            "yokohama": "横浜",
            "fujisawa": "藤沢",
            "kamakura": "鎌倉",
            "odawara": "小田原",
            "ofuna": "大船",
            "kawasaki": "川崎",
            "machida": "町田",
            "tachikawa": "立川",
            "hachioji": "八王子",
            "chiba": "千葉",
            "saitama": "さいたま",
            "omiya": "大宮",
            "osaka": "大阪",
            "kyoto": "京都",
            "nagoya": "名古屋",
            "kobe": "神戸",
            "hiroshima": "広島",
            "fukuoka": "福岡",
            "sapporo": "札幌",
            "sendai": "仙台",
            "nara": "奈良",
            "shinbashi": "新橋",
            "shimbashi": "新橋",
            "ochanomizu": "御茶ノ水",
            "ebisu": "恵比寿",
            "meguro": "目黒",
            "gotanda": "五反田",
            "tamachi": "田町",
            "hamamatsucho": "浜松町",
            "yurakucho": "有楽町",
            "kanda": "神田",
            "nippori": "日暮里",
            "tabata": "田端",
            "komagome": "駒込",
            "sugamo": "巣鴨",
            "otsuka": "大塚",
            "mejiro": "目白",
            "takadanobaba": "高田馬場",
            "shin-okubo": "新大久保",
            "yoyogi": "代々木",
            "harajuku": "原宿",
            "omotesando": "表参道",
            "roppongi": "六本木",
            "ginza": "銀座",
            "tsukiji": "築地",
            "asakusa": "浅草",
            "skytree": "スカイツリー",
            "narita": "成田",
            "haneda": "羽田",
            "mitaka": "三鷹",
            "kichijoji": "吉祥寺",
            "nakano": "中野",
            "ogikubo": "荻窪",
            "sagami-ono": "相模大野",
        }
        return romaji_map.get(v.lower().strip(), v)


class StationInfoInput(BaseModel):
    """Input for getting station information."""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")

    station_name: str = Field(
        ...,
        description="Station name in Japanese (e.g., '藤沢') or English (e.g., 'fujisawa')",
        min_length=1,
        max_length=100,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for readable text, 'json' for structured data",
    )

    @field_validator("station_name")
    @classmethod
    def convert_romaji(cls, v: str) -> str:
        return TransitSearchInput.convert_romaji(v)


# ─── Tools ────────────────────────────────────────────────────────────

@mcp.tool(
    name="yahoo_transit_search",
    annotations=ToolAnnotations(
        title="Search Transit Routes",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def yahoo_transit_search(
    origin: str,
    destination: str,
    date: Optional[str] = None,
    time: Optional[str] = None,
    search_type: int = 1,
    ticket_type: str = "ic",
    sort: str = "time",
    response_format: str = "markdown",
) -> str:
    """Search for train/bus routes between two stations in Japan via Yahoo Transit.

    Args:
        origin: Departure station in Japanese or English romaji (e.g. '藤沢' or 'fujisawa').
        destination: Arrival station in Japanese or English romaji (e.g. '秋葉原' or 'akihabara').
        date: Travel date in YYYY-MM-DD format. Defaults to today.
        time: Travel time in HH:MM 24h format (e.g. '08:30'). Defaults to now.
        search_type: 1=departure (default), 2=last train, 3=first train, 4=arrival.
        ticket_type: 'ic' for IC card fare (default), 'normal' for regular ticket.
        sort: 'time'=fastest (default), 'transfer'=fewest transfers, 'price'=cheapest.
        response_format: 'markdown' (default) or 'json'.

    Returns:
        Route results with departure/arrival times, fare, transfers, and train lines.
    """
    try:
        # Validate and convert inputs via the Pydantic model
        params = TransitSearchInput(
            origin=origin,
            destination=destination,
            date=date,
            time=time,
            search_type=SearchType(search_type),
            ticket_type=TicketType(ticket_type),
            sort=SortOrder(sort),
            response_format=ResponseFormat(response_format),
        )

        # Build query parameters
        query_params: dict[str, Any] = {
            "from": params.origin,
            "to": params.destination,
            "type": params.search_type.value,
            "ticket": params.ticket_type.value,
            "expkind": 1,
            "ws": 3,  # normal walking speed
        }

        sort_map = {"time": 0, "transfer": 1, "price": 2}
        query_params["s"] = sort_map.get(params.sort.value, 0)

        if params.date:
            y, m, d = params.date.split("-")
            query_params["y"] = y
            query_params["m"] = m
            query_params["d"] = d

        if params.time:
            hh, mm = params.time.split(":")
            query_params["hh"] = hh
            query_params["m1"] = mm[0]
            query_params["m2"] = mm[1]

        html = await _fetch_page(SEARCH_URL, params=query_params)
        routes = _parse_routes(html)

        if params.response_format == ResponseFormat.JSON:
            return _format_routes_json(params.origin, params.destination, routes)
        return _format_routes_markdown(params.origin, params.destination, routes)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="yahoo_transit_station_info",
    annotations=ToolAnnotations(
        title="Get Station Info",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def yahoo_transit_station_info(
    station_name: str,
    response_format: str = "markdown",
) -> str:
    """Get information about a train station in Japan from Yahoo Transit.

    Args:
        station_name: Station name in Japanese or English romaji (e.g. '藤沢' or 'fujisawa').
        response_format: 'markdown' (default) or 'json'.

    Returns:
        Station details including available rail lines.
    """
    try:
        params = StationInfoInput(
            station_name=station_name,
            response_format=ResponseFormat(response_format),
        )

        # Search for the station page
        search_url = f"{BASE_URL}/station/search"
        html = await _fetch_page(search_url, params={"q": params.station_name})
        soup = BeautifulSoup(html, "lxml")

        station_info: dict = {"name": params.station_name, "lines": [], "timetable_links": []}

        # Check if we were redirected to a station page directly
        # or if we got search results
        title_tag = soup.find("title")
        title = title_tag.get_text() if title_tag else ""

        if ("駅の情報" in title or "時刻表" in title or "駅情報" in title) and "検索結果" not in title:
            # Direct station page
            station_info["name"] = _clean_text(
                title.replace("の時刻表 路線一覧 - Yahoo!路線情報", "")
                     .replace(" - Yahoo!路線情報", "")
                     .replace("駅の情報 - ", "")
            )
            _extract_lines_from_station_page(soup, station_info)
        else:
            # Search results page - find station links
            results = []
            for link in soup.find_all("a", href=re.compile(r"/station/\d+")):
                name = _clean_text(link.get_text())
                if name and "駅を登録" not in name:
                    results.append({
                        "name": name,
                        "url": BASE_URL + link["href"],
                    })

            if results:
                # Get info for the first matching station
                first_url = results[0]["url"]
                station_info["name"] = results[0]["name"]

                detail_html = await _fetch_page(first_url)
                detail_soup = BeautifulSoup(detail_html, "lxml")
                _extract_lines_from_station_page(detail_soup, station_info)

                if len(results) > 1:
                    station_info["other_matches"] = [r["name"] for r in results[1:5]]

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(station_info, ensure_ascii=False, indent=2)

        # Markdown format
        lines = [f"# Station: {station_info['name']}", ""]
        if station_info["lines"]:
            lines.append("## Available Lines")
            for line_info in station_info["timetable_links"]:
                lines.append(f"- {line_info['line']}")
            lines.append("")
        else:
            lines.append("No line information found.")

        if station_info.get("other_matches"):
            lines.append("## Other Matching Stations")
            for m in station_info["other_matches"]:
                lines.append(f"- {m}")

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


if __name__ == "__main__":
    mcp.run()
