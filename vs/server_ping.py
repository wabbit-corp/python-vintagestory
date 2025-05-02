import asyncio
import struct
import logging
from dataclasses import dataclass

# ------------------------------------------------------------------------------
# Dataclass for the parsed query answer

@dataclass
class ServerQueryAnswer:
    name: str
    motd: str
    player_count: int
    max_players: int
    game_mode: str
    password: bool
    server_version: str

# ------------------------------------------------------------------------------
# Helpers to parse a string in our assumed format

def read_string(data: bytes, offset: int) -> (str, int):
    """
    Reads a string from the binary data.
    Expects a 4-byte big-endian length followed by UTF-8 encoded bytes.
    Returns a tuple: (decoded_string, new_offset)
    """
    if len(data) < offset + 4:
        raise ValueError("Not enough data to read string length")
    (length,) = struct.unpack_from(">I", data, offset)
    offset += 4
    if len(data) < offset + length:
        raise ValueError("Not enough data to read string content")
    s = data[offset:offset+length].decode("utf-8")
    offset += length
    return s, offset

def parse_server_query_answer(data: bytes) -> ServerQueryAnswer:
    """
    Parses the Packet_ServerQueryAnswer from the given data.
    Assumed layout (in order):
      - string Name         (field 1, tag 0x0A)
      - string MOTD         (field 2, tag 0x12)  -- may be omitted if empty
      - int32 PlayerCount   (field 3, tag 0x18)
      - int32 MaxPlayers    (field 4, tag 0x20)
      - string GameMode     (field 5, tag 0x2A)
      - bool Password       (field 6, tag 0x30)  -- may be omitted, default false
      - string ServerVersion(field 7, tag 0x3A)

    Note that if a field is omitted, we assume a default value.
    This simple parser works only if the fields appear in order.
    """
    offset = 0

    # Field 1: Name
    # Expecting tag 0x0A followed by a varint length and the string bytes.
    if data[offset] != 0x0A:
        raise ValueError("Expected field 1 (Name) tag 0x0A at offset 0")
    offset += 1
    # Read length (assumed to be encoded as a single byte varint)
    name_length = data[offset]
    offset += 1
    if len(data) < offset + name_length:
        raise ValueError("Not enough data to read Name content")
    name = data[offset:offset+name_length].decode("utf-8")
    offset += name_length

    # Field 2: MOTD (optional)
    motd = ""
    if offset < len(data) and data[offset] == 0x12:
        offset += 1
        motd_length = data[offset]
        offset += 1
        if len(data) < offset + motd_length:
            raise ValueError("Not enough data to read MOTD content")
        motd = data[offset:offset+motd_length].decode("utf-8")
        offset += motd_length

    # Field 3: PlayerCount (tag 0x18, varint)
    if offset < len(data) and data[offset] == 0x18:
        offset += 1
        # We assume PlayerCount is small enough to be in one byte.
        player_count = data[offset]
        offset += 1
    else:
        raise ValueError("Expected field 3 (PlayerCount) tag 0x18")

    # Field 4: MaxPlayers (tag 0x20, varint)
    if offset < len(data) and data[offset] == 0x20:
        offset += 1
        max_players = data[offset]
        offset += 1
    else:
        raise ValueError("Expected field 4 (MaxPlayers) tag 0x20")

    # Field 5: GameMode (tag 0x2A, length-delimited)
    if offset < len(data) and data[offset] == 0x2A:
        offset += 1
        gamemode_length = data[offset]
        offset += 1
        if len(data) < offset + gamemode_length:
            raise ValueError("Not enough data to read GameMode content")
        game_mode = data[offset:offset+gamemode_length].decode("utf-8")
        offset += gamemode_length
    else:
        raise ValueError("Expected field 5 (GameMode) tag 0x2A")

    # Field 6: Password (tag 0x30, varint) -- optional, default to False
    password = False
    if offset < len(data) and data[offset] == 0x30:
        offset += 1
        # Assume one-byte boolean (0 or 1)
        password = (data[offset] != 0)
        offset += 1

    # Field 7: ServerVersion (tag 0x3A, length-delimited)
    if offset < len(data) and data[offset] == 0x3A:
        offset += 1
        ver_length = data[offset]
        offset += 1
        if len(data) < offset + ver_length:
            raise ValueError("Not enough data to read ServerVersion content")
        server_version = data[offset:offset+ver_length].decode("utf-8")
        offset += ver_length
    else:
        raise ValueError("Expected field 7 (ServerVersion) tag 0x3A")

    return ServerQueryAnswer(
        name=name,
        motd=motd,
        player_count=player_count,
        max_players=max_players,
        game_mode=game_mode,
        password=password,
        server_version=server_version
    )

# ------------------------------------------------------------------------------
# Functions to build our minimal Packet_Client

def build_query_packet() -> bytes:
    """
    Constructs a minimal Packet_Client that will trigger the server query.
    In the Cito serialization, Packet_Client.Id is field number 1 (with wire type 0),
    so the key is 0x08. For a query we set Id to 15 (0x0F).

    Without length prefix, the payload is:
        b"\x08\x0F"

    Then we add a 4-byte length prefix (big-endian) before it.
    """
    payload = b"\x08\x0F"  # field key 0x08 then value 0x0F (15)
    length_prefix = struct.pack(">I", len(payload))
    return length_prefix + payload

# ------------------------------------------------------------------------------
# Async client implementation

async def query_server(host: str, port: int) -> ServerQueryAnswer:
    """
    Connects to the server, sends our query packet, and reads the response.
    We assume that the server frames packets with a 4-byte length prefix.
    """
    logger = logging.getLogger("server_query")
    reader, writer = await asyncio.open_connection(host, port)
    logger.info(f"Connected to {host}:{port}")

    # Build and send the query packet (with length prefix)
    packet = build_query_packet()
    writer.write(packet)
    await writer.drain()
    logger.info("Sent query packet (id 15)")

    # Read one packet from the server using a 4-byte length prefix
    async def read_packet() -> bytes:
        header = await reader.readexactly(4)
        (packet_length,) = struct.unpack(">I", header)
        payload = await reader.readexactly(packet_length)
        return payload

    try:
        payload = await read_packet()
        logger.info(f"Received a packet with {len(payload)} bytes")
        logger.debug(f"Packet data: {payload.hex()}")

        # The payload appears to have a 6-byte header before the actual QueryAnswer.
        # Look for the first occurrence of b'\x0a' which is our expected tag for field 1 (Name).
        idx = payload.find(b"\x0a")
        if idx < 0:
            raise ValueError("Could not find start of QueryAnswer (0x0a tag)")
        inner_payload = payload[idx:]
        logger.info(f"Stripped header, QueryAnswer payload is {len(inner_payload)} bytes")
        logger.debug(f"QueryAnswer data: {inner_payload.hex()}")

        answer = parse_server_query_answer(inner_payload)
        logger.info("Parsed server query answer successfully")
    except Exception as e:
        logger.error(f"Error parsing response: {e}")
        raise

    writer.close()
    await writer.wait_closed()
    return answer

# ------------------------------------------------------------------------------
# Main entry point

async def main():
    logging.basicConfig(level=logging.DEBUG)
    # Replace these with the actual server host and port.
    host = "88.99.139.214"
    port = 42420

    # 199.115.77.163:26915
    host = "199.115.77.163"
    port = 26915

    try:
        answer = await query_server(host, port)
        logging.info(f"Server Name: {answer.name}")
        logging.info(f"MOTD: {answer.motd}")
        logging.info(f"Players: {answer.player_count} / {answer.max_players}")
        logging.info(f"Game Mode: {answer.game_mode}")
        logging.info(f"Password Protected: {answer.password}")
        logging.info(f"Server Version: {answer.server_version}")
    except Exception as e:
        logging.error(f"Error querying server: {e}")

if __name__ == "__main__":
    asyncio.run(main())
