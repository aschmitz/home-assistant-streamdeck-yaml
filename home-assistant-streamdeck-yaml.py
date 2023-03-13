"""Home Assistant Stream Deck integration."""
from __future__ import annotations

import asyncio
import functools
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import cairosvg
import requests
import rich
import websockets
import yaml
from jinja2 import Template
from lxml import etree
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from StreamDeck.Devices import StreamDeck

ASSETS_PATH = Path(__file__).parent / "assets"

DEFAULT_MDI_ICONS = {"light": "lightbulb", "switch": "power-socket-eu"}

_ID_COUNTER = 0


class Button(BaseModel, extra="forbid"):  # type: ignore[call-arg]
    """Button configuration."""

    entity_id: str | None = None
    service: str | None = None
    service_data: dict[str, Any] = Field(default_factory=dict)
    text: str = ""
    text_color: str | None = None
    text_size: int = 12
    icon: str | None = None
    icon_mdi: str | None = None

    @property
    def domain(self) -> str | None:
        """Return the domain of the entity."""
        if self.service is None:
            return None
        return self.service.split(".", 1)[0]


class Config(BaseModel):
    """Configuration file."""

    buttons: list[Button] = Field(default_factory=list)


def _next_id() -> int:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return _ID_COUNTER


@asynccontextmanager
async def setup_ws(host: str, token: str) -> websockets.WebSocketClientProtocol:
    """Set up the connection to Home Assistant."""
    uri = f"wss://{host}/api/websocket"
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                # Send an authentication message to Home Assistant
                auth_payload = {"type": "auth", "access_token": token}
                await websocket.send(json.dumps(auth_payload))

                # Wait for the authentication response
                auth_response = await websocket.recv()
                rich.print(auth_response)
                rich.print("Connected to Home Assistant")
                yield websocket
        except ConnectionResetError:
            # Connection was reset, retrying in 3 seconds
            rich.print("Connection was reset, retrying in 3 seconds")
            await asyncio.sleep(5)


async def subscribe_state_changes(
    websocket: websockets.WebSocketClientProtocol,
) -> None:
    """Subscribe to the state change events."""
    subscribe_payload = {
        "type": "subscribe_events",
        "event_type": "state_changed",
        "id": _next_id(),
    }
    await websocket.send(json.dumps(subscribe_payload))


async def handle_state_changes(
    websocket: websockets.WebSocketClientProtocol,
    state: dict[str, Any],
    deck: StreamDeck,
    buttons: list[Button],
) -> None:
    """Handle state changes."""
    # Wait for the state change events
    while True:
        data = json.loads(await websocket.recv())
        _update_state(state, data, buttons, deck)


def _keys(entity_id: str, buttons: list[Button]) -> list[int]:
    """Get the key indices for an entity_id."""
    return [i for i, button in enumerate(buttons) if button.entity_id == entity_id]


def _update_state(
    state: dict[str, Any],
    data: dict[str, Any],
    buttons: list[Button],
    deck: StreamDeck,
) -> None:
    """Update the state dictionary and update the keys."""
    if data["type"] == "event":
        event_data = data["event"]
        if event_data["event_type"] == "state_changed":
            event_data = event_data["data"]
            eid = event_data["entity_id"]
            state[eid] = event_data["new_state"]
            keys = _keys(eid, buttons)
            for key in keys:
                rich.print(f"Updating key {key} for {eid}")
                update_key_image(
                    deck,
                    key=key,
                    button=buttons[key],
                    state=state,
                    key_pressed=False,
                )


def _render_jinja(text: str, data: dict[str, Any]) -> str:
    """Render a Jinja template."""
    template = Template(text)
    return template.render(**data)


async def get_states(websocket: websockets.WebSocketClientProtocol) -> dict[str, Any]:
    """Get the current state of all entities."""
    _id = _next_id()
    subscribe_payload = {
        "type": "get_states",
        "id": _id,
    }
    await websocket.send(json.dumps(subscribe_payload))
    while True:
        data = json.loads(await websocket.recv())
        if data["type"] == "result":
            # Extract the state data from the response
            state_dict = {state["entity_id"]: state for state in data["result"]}
            rich.print(state_dict)
            await unsubscribe(websocket, _id)
            return state_dict


async def unsubscribe(websocket: websockets.WebSocketClientProtocol, id_: int) -> None:
    """Unsubscribe from an event."""
    subscribe_payload = {
        "id": _next_id(),
        "type": "unsubscribe_events",
        "subscription": id_,
    }
    await websocket.send(json.dumps(subscribe_payload))


async def call_service(
    websocket: websockets.WebSocketClientProtocol,
    service: str,
    data: dict[str, Any],
) -> None:
    """Call a service."""
    domain, service = service.split(".")
    subscribe_payload = {
        "id": _next_id(),
        "type": "call_service",
        "domain": domain,
        "service": service,
        "service_data": data,
    }
    await websocket.send(json.dumps(subscribe_payload))


def _named_to_hex(color: str) -> str:
    """Convert a named color to a hex color."""
    rgb: tuple[int, int, int] = ImageColor.getrgb(color)
    color = "#{:02x}{:02x}{:02x}".format(*rgb)
    if color.startswith("#"):
        return color
    msg = f"Invalid color: {color}"
    raise ValueError(msg)


def render_key_image(
    deck: StreamDeck,
    *,
    text_color: str = "white",
    icon_filename: str | None = None,
    icon_mdi: str | None = None,
    icon_mdi_margin: int = 0,
    font_filename: str = "Roboto-Regular.ttf",
    font_size: int = 12,
    label_text: str = "",
) -> memoryview:
    """Render an image for a key."""
    if icon_filename is not None:
        icon = Image.open(ASSETS_PATH / icon_filename)
    elif icon_mdi is not None:
        url = _mdi_url(icon_mdi)
        _download_and_convert_svg_to_png(
            url,
            color=_named_to_hex(text_color),
            opacity=0.3,
            filename=ASSETS_PATH / f"{icon_mdi}.png",
            margin=icon_mdi_margin,
        )
        icon = Image.open(ASSETS_PATH / f"{icon_mdi}.png")
    else:
        icon = Image.new("RGB", (deck.KEY_PIXEL_WIDTH, deck.KEY_PIXEL_HEIGHT), "black")
    image = PILHelper.create_scaled_image(deck, icon, margins=[0, 0, 0, 0])
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(ASSETS_PATH / font_filename), font_size)
    draw.text(
        (image.width / 2, image.height / 2),
        text=label_text,
        font=font,
        anchor="ms",
        fill=text_color,
        align="center",
    )
    return PILHelper.to_native_format(deck, image)


def update_key_image(
    deck: StreamDeck,
    *,
    key: int,
    button: Button,
    state: dict[str, Any],
    key_pressed: bool = False,
) -> None:
    """Update the image for a key."""
    if button.entity_id in state:
        # Has entity_id
        state = state[button.entity_id]
        text = _render_jinja(button.text, data={"state": state})
        if button.text_color is not None:
            text_color = _render_jinja(button.text_color, data={"state": state})
        elif state["state"] == "on":
            text_color = "orangered"
        else:
            text_color = "white"

        if button.icon_mdi is not None:
            icon_mdi = button.icon_mdi
        elif button.icon is None and button.domain in DEFAULT_MDI_ICONS:
            icon_mdi = DEFAULT_MDI_ICONS[button.domain]
        else:
            icon_mdi = None
    else:
        # No entity_id, e.g., a script
        text = button.text
        text_color = button.text_color or "white"
        icon_mdi = button.icon_mdi
    image = render_key_image(
        deck,
        label_text=text,
        text_color=text_color if not key_pressed else "green",
        icon_mdi=icon_mdi,
        icon_filename=button.icon,
        font_size=button.text_size,
    )
    with deck:
        deck.set_key_image(key, image)


def get_deck() -> StreamDeck:
    """Get the first Stream Deck device found on the system."""
    streamdecks = DeviceManager().enumerate()
    found = False
    for deck in streamdecks:
        if not deck.is_visual():
            continue
        deck.open()
        deck.reset()
        found = True
        break
    if not found:
        msg = "No Stream Deck found"
        raise RuntimeError(msg)
    print(f"Found {deck.key_count()} keys, {deck=}")
    return deck


def read_config(fname: Path) -> Config:
    """Read the configuration file."""
    with fname.open() as f:
        return Config(buttons=yaml.safe_load(f))


def _on_press_callback(
    websocket: websockets.WebSocketClientProtocol,
    state: dict[str, Any],
    buttons: list[Button],
) -> Callable[[StreamDeck, int, bool], Coroutine[StreamDeck, int, None]]:
    async def key_change_callback(
        deck: StreamDeck,
        key: int,
        key_pressed: bool,  # noqa: FBT001
    ) -> None:
        print(f"Key {key} {'pressed' if key_pressed else 'released'}")
        button = buttons[key]
        update_key_image(
            deck,
            key=key,
            button=button,
            state=state,
            key_pressed=key_pressed,
        )
        if key_pressed and button.service is not None:
            data = button.service_data
            if not button.service_data and button.entity_id is not None:
                # Add the entity_id to the service data if service_data is empty
                data = {"entity_id": button.entity_id}
            rich.print(f"Calling service {button.service} with data {data}")
            await call_service(websocket, button.service, data)

    return key_change_callback


@functools.lru_cache(maxsize=128)
def _download(url: str) -> bytes:
    """Download the content from the URL."""
    response = requests.get(url, timeout=5)
    return response.content


def _scale_hex_color(hex_color: str, scale: float) -> str:
    """Scales a HEX color by a given factor.

    Parameters
    ----------
    hex_color
        A HEX color in the format "#RRGGBB".
    scale
        A scaling factor between 0 and 1.

    Returns
    -------
    A scaled HEX color in the format "#RRGGBB".
    """
    # Convert HEX color to RGB values
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)

    # Scale RGB values
    r = int(r * scale)
    g = int(g * scale)
    b = int(b * scale)

    # Convert scaled RGB values back to HEX color
    return f"#{r:02x}{g:02x}{b:02x}"


def _download_and_convert_svg_to_png(
    url: str,
    color: str,
    opacity: float,
    filename: str | Path,
    margin: int,
) -> None:
    """Download an SVG file from a given URL to PNG.

    Modify the fill and background colors based on the input color value,
    convert it to PNG format, and save the resulting PNG image to a file.

    Parameters
    ----------
    url
        The URL of the SVG file.
    color
        The HEX color to use for the icon.
    opacity
        The opacity of the icon. 0 is black, 1 is full color.
    filename
        The name of the file to save the PNG content to.
    margin
        The margin to add around the icon.
    """
    svg_content = _download(url)

    # Modify the SVG content to set the fill and background colors
    svg_tree = etree.fromstring(svg_content)
    fill_color = _scale_hex_color(color, opacity)
    svg_tree.attrib["fill"] = fill_color
    svg_tree.attrib["style"] = "background-color: #000000"
    modified_svg_content = etree.tostring(svg_tree)

    # Convert the modified SVG content to PNG format using cairosvg
    png_content = cairosvg.svg2png(
        bytestring=modified_svg_content,
        background_color="#000000",
        scale=4,
    )

    # Save the resulting PNG image to a file using Pillow
    with Image.open(io.BytesIO(png_content)) as image:
        im = ImageOps.expand(image, border=(margin, margin), fill="black")
        im = im.resize((72, 72))
        im.save(filename)


def _mdi_url(mdi: str) -> str:
    """Return the URL of the Materian. opacity=Design Ico,.

    Check https://mdi.bessarabov.com for the available icons.
    """
    return f"https://raw.githubusercontent.com/Templarian/MaterialDesign/master/svg/{mdi}.svg"


async def main(host: str, token: str, config: Config) -> None:
    """Main entry point for the Stream Deck integration."""
    deck = get_deck()
    buttons = config.buttons  # TODO: make it work with multiple decks
    async with setup_ws(host, token) as websocket:
        state = await get_states(websocket)
        for key in range(deck.key_count()):
            update_key_image(
                deck,
                key=key,
                button=buttons[key],
                state=state,
                key_pressed=False,
            )
        deck.set_key_callback_async(_on_press_callback(websocket, state, buttons))
        deck.set_brightness(100)
        await subscribe_state_changes(websocket)
        await handle_state_changes(websocket, state, deck, buttons)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--token", default=None)
    parser.add_argument("--config", default="streamdeck-config.yaml", type=Path)
    args = parser.parse_args()
    config = read_config(args.config)
    asyncio.run(main(host=args.host, token=args.token, config=config))
