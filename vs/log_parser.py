from __future__ import annotations
from typing import List, Dict, Union, Generator, Any, Callable
from datetime import datetime
from dataclasses import dataclass
import dataclasses
import termcolor

import io
import re

# 27.1.2025 21:57:21 [Server Notification]
# 27.1.2025 21:57:21 [Server Event]

LINE_RE = re.compile(r'(\d+\.\d+\.\d+) (\d+:\d+:\d+) \[(.*?)\] (.*)')

@dataclass
class LogLine:
    raw_line: str
    date: datetime
    type: str
    raw_message: str
    parsed_message: ParsedMessage | None

class ParsedMessage:
    pass


@dataclass
class Pattern:
    pattern: re.Pattern
    parser: Callable[[re.Match, str], ParsedMessage]


def parse_log_file(file: str | io.TextIOBase) -> Generator[LogLine, None, None]:
    if isinstance(file, str):
        with open(file, 'rt', encoding='utf-8') as f:
            yield from parse_log_file(f)
        return

    assert isinstance(file, io.TextIOBase)

    lines = file.readlines()

    # We want to parse multi-line messages
    buffer: List[str] = []
    for line in lines:
        match = LINE_RE.match(line)
        if match:
            if buffer:
                yield parse_log_line(buffer)
                buffer = []
        buffer.append(line)

    if buffer:
        yield parse_log_line(buffer)


def parse_log_line(lines: List[str]) -> LogLine:
    raw_line = '\n'.join(lines)

    m = LINE_RE.match(lines[0])
    if not m:
        raise ValueError('Invalid log line')
    date, time, type, raw_message = m.groups()
    date = datetime.strptime(f'{date} {time}', '%d.%m.%Y %H:%M:%S')

    raw_message = ''.join([raw_message] + lines[1:])
    return LogLine(raw_line, date, type, raw_message, parse_message(date, type, raw_message))


PATTERNS: List[Pattern] = []

COLOR_MAP = {
    'Server Notification': 'green',
    'Server Event': 'cyan',
    'Server Chat': 'blue',
    'Server Command': 'magenta',
    'Server Debug': 'white',
    'Server Warning': 'yellow',
    'Server Error': 'red',
    # other colors: grey, magenta, cyan, white
}

TYPE_MAP: Dict[str, str] = {
    'Server Notification': 'Notification',
    'Server Event': 'Event',
    'Server Chat': 'Chat',
    'Server Command': 'Command',
    'Server Debug': 'Debug',
    'Server Warning': 'Warning',
    'Server Error': 'Error',
}


def parse_message(date: datetime, type: str, raw_message: str) -> ParsedMessage | None:
    for pattern in PATTERNS:
        m = pattern.pattern.search(raw_message)
        if m:
            return pattern.parser(m, raw_message)
    return None


###############################################################################
# Player events
###############################################################################

# 27.1.2025 22:26:58 [Server Event] WolfclawSDemon [::ffff:172.58.121.252]:15196 joins.
# DuckBandlt [::ffff:24.34.251.150]:54914 joins.

@dataclass
class PlayerJoin(ParsedMessage):
    player: str
    ip: str
    port: int

PATTERNS.append(Pattern(
    pattern=re.compile(
        r"(?P<player>.*) \[(?P<ip>.*)\]:(?P<port>\d+) joins.",
        re.IGNORECASE),
    parser=lambda m, raw: PlayerJoin(
        player=m.group("player"),
        ip=m.group("ip"),
        port=int(m.group("port")),
    )
))

# 27.1.2025 22:26:06 [Server Notification] Placing Hades89 at 510804.27935791016 155 513060.4216308594

@dataclass
class PlayerPlacement(ParsedMessage):
    player: str
    x: float
    y: float
    z: float

PATTERNS.append(Pattern(
    pattern=re.compile(
        r"Placing (?P<player>.*) at (?P<x>[\d.]+) (?P<y>[\d.]+) (?P<z>[\d.]+)",
        re.IGNORECASE),
    parser=lambda m, raw: PlayerPlacement(
        player=m.group("player"),
        x=float(m.group("x")),
        y=float(m.group("y")),
        z=float(m.group("z")),
    )
))

# Player DoofusDan left.

@dataclass
class PlayerLeave(ParsedMessage):
    player: str

PATTERNS.append(Pattern(
    pattern=re.compile(
        r"Player (?P<player>.*) left.",
        re.IGNORECASE),
    parser=lambda m, raw: PlayerLeave(
        player=m.group("player"),
    )
))

# [playercorpse] Created Gearalt's corpse at x=953, y=214, z=928, id 1067765

@dataclass
class PlayerCorpse(ParsedMessage):
    player: str
    x: int
    y: int
    z: int
    id: int

PATTERNS.append(Pattern(
    pattern=re.compile(
        r"\[playercorpse\] Created (?P<player>.*)'s corpse at x=(?P<x>\d+), y=(?P<y>\d+), z=(?P<z>\d+), id (?P<id>\d+)",
        re.IGNORECASE),
    parser=lambda m, raw: PlayerCorpse(
        player=m.group("player"),
        x=int(m.group("x")),
        y=int(m.group("y")),
        z=int(m.group("z")),
        id=int(m.group("id")),
    )
))

# [playercorpse] Gearalt's corpse at x=953, y=213.125, z=928 was destroyed, id 1067765

@dataclass
class PlayerCorpseDestroyed(ParsedMessage):
    player: str
    x: int
    y: float
    z: int
    id: int

PATTERNS.append(Pattern(
    pattern=re.compile(
        r"\[playercorpse\] (?P<player>.*)'s corpse at x=(?P<x>\d+), y=(?P<y>[\d.]+), z=(?P<z>\d+) was destroyed, id (?P<id>\d+)",
        re.IGNORECASE),
    parser=lambda m, raw: PlayerCorpseDestroyed(
        player=m.group("player"),
        x=int(m.group("x")),
        y=float(m.group("y")),
        z=int(m.group("z")),
        id=int(m.group("id")),
    )
))

# Chat Messages
# 27.1.2025 23:24:03 [Server Chat] 0 | DoofusDan: The Sod house is nice and cozy though.
# 27.1.2025 23:22:43 [Server Chat] 0 | uarehere: temp just went down to -8 for me

@dataclass
class ChatMessage(ParsedMessage):
    player: str
    message: str

PATTERNS.append(Pattern(
    pattern=re.compile(
            r"\d+ \| (?P<player>[^:]+): (?P<message>.*)",
            re.IGNORECASE),
    parser=lambda m, raw: ChatMessage(
            player=m.group("player"),
            message=m.group("message"),
)))

###############################################################################
# Server events
###############################################################################

# 30.1.2025 03:47:25 [Server Warning] Server overloaded. A tick took 563ms to complete.

class ServerLifecycleEvent(ParsedMessage):
    pass

@dataclass
class ServerOverloaded(ServerLifecycleEvent):
    tick_duration: int

PATTERNS.append(Pattern(
    pattern=re.compile(
            r"Server overloaded. A tick took (?P<tick_duration>\d+)ms to complete.",
            re.IGNORECASE),
    parser=lambda m, raw: ServerOverloaded(
            tick_duration=int(m.group("tick_duration")),
)))

# 30.1.2025 04:01:46 [Server Notification] Handling Console Command /stop

@dataclass
class ConsoleCommand(ParsedMessage):
    command: str

PATTERNS.append(Pattern(
    pattern=re.compile(
            r"Handling Console Command (?P<command>.*)",
            re.IGNORECASE),
    parser=lambda m, raw: ConsoleCommand(
            command=m.group("command"),
)))

# 30.1.2025 03:44:45 [Server Error] Exception: Object reference not set to an instance of an object.
#    at Vintagestory.Server.CmdPlayer.setMovespeed(PlayerUidName targetPlayer, TextCommandCallingArgs args) in VintagestoryLib\Server\Systems\Player\CmdPlayer.cs:line 1086
#    at Vintagestory.Server.CmdPlayer.Each(TextCommandCallingArgs args, PlayerEachDelegate onPlayer) in VintagestoryLib\Server\Systems\Player\CmdPlayer.cs:line 1113

@dataclass
class Exception(ParsedMessage):
    message: str
    stacktrace: List[str]

PATTERNS.append(Pattern(
    pattern=re.compile(
            r"Exception: (?P<message>.*)\n(?P<stacktrace>.*\n)+",
            re.IGNORECASE),
    parser=lambda m, raw: Exception(
            message=m.group("message"),
            stacktrace=[s for s in m.group("stacktrace").split('\n') if s],
)))


if __name__ == '__main__':
    import os, sys, json

    if sys.platform.lower() == "win32":
        os.system('color')
        os.system('chcp 65001 > nul')
        sys.stdout.reconfigure(encoding='utf-8') # type: ignore
        sys.stderr.reconfigure(encoding='utf-8') # type: ignore

    class DataclassEncoder(json.JSONEncoder):
        def default(self, o: Any) -> Any:
            if dataclasses.is_dataclass(o):
                return dataclasses.asdict(o) # type: ignore
            return super().default(o)

    for line in parse_log_file('2025-01-28_06-33-21-VintageStory.log'):
        adjusted_type = TYPE_MAP.get(line.type, line.type)

        if line.parsed_message:
            j = json.dumps(line.parsed_message, cls=DataclassEncoder)
            msg = f"{line.date} [{adjusted_type}] {j}"
        else:
            msg = f"{line.date} [{adjusted_type}] {line.raw_message}"

        color = COLOR_MAP.get(line.type, 'grey')
        print(termcolor.colored(msg, color))
