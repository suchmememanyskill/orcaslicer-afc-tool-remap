# /// script
# requires-python = ">=3.12"
# [tool.orcaslicer.plugin]
# name = "AFC tool remap"
# description = "Review physical AFC lane assignments before Orca uploads G-code."
# author = "Sims"
# version = "0.3.18"
# type = "slicing-pipeline"
# ///
"""Orca 2.5 experimental AFC tool remap mapping. No uploader or G-code edits.

Interactive worker hooks have limited host cancellation support. A UI heartbeat
bounds waits when the GUI stops servicing callbacks; see README before testing.
"""
import base64
import csv
import math
from pathlib import Path
import queue
import sys
import threading
import json
import re
import struct
import time
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


def tool_number(value):
    text = str(value)
    if not re.fullmatch(r"T?\d+", text):
        raise ValueError("Invalid AFC tool")
    return int(text.removeprefix("T"))


def physical_lane_tools(names, response):
    """Read each physical lane's mapping; AFC.maps is only a tool registry."""
    status = response["result"]["status"]
    current = {}
    for name in names:
        mappings = status["AFC_stepper " + name]["map"]
        if not isinstance(mappings, list) or len(mappings) != 1:
            raise ValueError(f"Physical lane {name} does not have a single tool mapping")
        current[name] = tool_number(mappings[0])
    if len(set(current.values())) != len(current):
        raise ValueError("Duplicate AFC tool")
    return current


def parse_lanes(database, afc, units, lane_response):
    """Join database metadata by mutable tool, never by database key."""
    records = {}
    for record in database["result"]["value"].values():
        tool = tool_number(record["lane"])
        if tool in records:
            raise ValueError("Duplicate database tool")
        records[tool] = record
    status = afc["result"]["status"]["AFC"]
    if status.get("multiple_tool_mapping", False):
        raise ValueError("Multiple tool mapping is enabled; this plugin currently supports one-to-one mapping only")
    names = status["lanes"]
    if not names or len(set(names)) != len(names):
        raise ValueError("Invalid AFC lane names")
    current = physical_lane_tools(names, lane_response)
    lanes = []
    for section in status["units"]:
        obj = "AFC_" + section
        for position, name in enumerate(units[obj]["result"]["status"][obj]["lanes"]):
            if name not in current or any(lane["name"] == name for lane in lanes):
                raise ValueError("Invalid unit membership")
            record = records.get(current[name], {})
            if str(record.get("extruder_index", 0)) != "0":
                raise ValueError("This plugin supports one physical extruder only")
            # AFC represents unassigned lane metadata as JSON null as well as
            # empty strings. Keep empty lanes visible, but not selectable.
            metadata = {}
            for field in ("material", "color"):
                value = record.get(field)
                if value is None:
                    value = ""
                if not isinstance(value, str):
                    raise ValueError(f"Invalid AFC {field} for physical lane {name}")
                metadata[field] = value
            weight = record.get("weight")
            remaining = (float(weight) if isinstance(weight, (int, float)) and not isinstance(weight, bool)
                         and math.isfinite(weight) and weight >= 0 else None)
            lanes.append(dict(name=name, unit=section, position=position,
                              tool=current[name], remaining_grams=remaining, **metadata))
    if len(lanes) != len(names):
        raise ValueError("Incomplete unit membership")
    return lanes


def plan(lanes, selections):
    """Return sequential SET_MAP swaps and the complete expected permutation.

    Recompute the simulated mapping after EVERY swap, including displaced lanes.
    selections maps outgoing zero-based T numbers to physical lane names.
    """
    by_name = {lane["name"]: lane for lane in lanes}
    after = {lane["tool"]: lane["name"] for lane in lanes}
    if not selections or len(set(selections.values())) != len(selections):
        raise ValueError("Select a different lane for each used tool")
    for tool, name in selections.items():
        if tool not in after or name not in by_name:
            raise ValueError("Unknown tool or lane")
        if not str(by_name[name]["material"]).strip():
            raise ValueError("Selected lane has no material")
        if not re.fullmatch(r"[^\s\x00-\x20\x7f#;'\"\\]+", name):
            raise ValueError("Unsafe lane name")
    commands = []
    for tool, name in sorted(selections.items()):
        other = next(t for t, lane in after.items() if lane == name)
        if other != tool:
            commands.append(f"SET_MAP LANE={name} MAP=T{tool}")
            after[tool], after[other] = after[other], after[tool]
    return commands, after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep mapping commands on the configured printer endpoint.
        return None


class Moonraker:
    def __init__(self, address, api_key=""):
        self.address = address.rstrip("/")
        if not self.address.startswith(("http://", "https://")):
            self.address = "http://" + self.address
        self.api_key = api_key
        self.mapping_write_attempted = False
        self.opener = build_opener(NoRedirect())

    def request_network_access(self):
        # Orca matches HTTP URLs only against a canonical scheme://host
        # grant. A bare IP grant covers connection events but not URL events.
        # Paths, ports and wildcards would prevent the host-wide URL match.
        address = urlsplit(self.address)
        host = address.hostname
        if ":" in host:
            host = "[" + host + "]"
        origin = urlunsplit((address.scheme, host, "", "", ""))
        sys.audit("urllib.Request", origin, None, {}, "GET")

    def request(self, path, payload=None):
        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        with self.opener.open(Request(self.address + path, data=data, headers=headers), timeout=5) as response:
            result = json.load(response)
        if "error" in result:
            raise ValueError("Moonraker returned an error")
        return result

    def query(self, obj):
        return self.request("/printer/objects/query?" + quote(obj, safe=""))

    def query_lane_maps(self, names):
        # Query all physical lane objects in one request, including their map lists.
        return self.request("/printer/objects/query?" + "&".join(
            quote("AFC_stepper " + name, safe="") for name in names))

    def fetch(self):
        afc = self.query("AFC")
        units = {"AFC_" + section: self.query("AFC_" + section)
                 for section in afc["result"]["status"]["AFC"]["units"]}
        lane_response = self.query_lane_maps(afc["result"]["status"]["AFC"]["lanes"])
        database = self.request("/server/database/item?namespace=lane_data")
        return parse_lanes(database, afc, units, lane_response)

    def ensure_idle(self):
        state = self.query("print_stats")["result"]["status"]["print_stats"]["state"]
        if state not in ("standby", "complete", "cancelled", "error"):
            raise ValueError("Printer must be idle before changing AFC mapping")

    def apply(self, snapshot, selections, cancelled=lambda: False):
        # No retries of mutations. A failure can leave a partial mapping persisted.
        self.ensure_idle()
        fresh = self.fetch()
        if fresh != snapshot:
            raise ValueError("AFC changed; review a fresh snapshot")
        commands, expected = plan(fresh, selections)
        if cancelled():
            raise ValueError("Send cancelled before applying mapping")
        if commands:
            payload = {"script": "\n".join(commands)}
            self.mapping_write_attempted = True
            self.request("/printer/gcode/script", payload)
        for attempt in range(6):
            if cancelled():
                raise ValueError("Send cancelled; mapping may already have changed")
            names = [lane["name"] for lane in fresh]
            current = physical_lane_tools(names, self.query_lane_maps(names))
            actual = {tool: name for name, tool in current.items()}
            if actual == expected:
                return
            if attempt < 5:
                time.sleep(0.2)
        raise ValueError("AFC mapping verification failed; upload must remain cancelled")


def profile_values(value):
    """Orca serializes string vectors with semicolons and optional quotes."""
    if not value:
        return []
    return next(csv.reader([str(value)], delimiter=";", skipinitialspace=True))


def project_filaments(path, get, details=None):
    stats = {}
    emitted = set()
    thumbnail = None
    thumbnail_area = 0
    collecting = None
    encoded_size = 0
    wanted = {"filament used [mm]", "filament used [g]", "filament_type", "filament_colour",
              "total filament used [g]", "total filament change", "estimated printing time (normal mode)"}
    with open(path, encoding="utf-8", errors="strict") as stream:
        for line in stream:
            match = re.match(r"^;\s*([^=]+?)\s*=\s*(.*?)\s*$", line)
            if match and match[1] in wanted:
                stats[match[1]] = match[2]
            tool = re.match(r"^\s*T(\d+)(?=\s|;|$)", line)
            if tool:
                emitted.add(int(tool[1]))
            begin = re.match(r"^;\s*thumbnail begin (\d+)x(\d+)", line)
            if begin:
                width, height = int(begin[1]), int(begin[2])
                area = width * height
                collecting = [] if 0 < width <= 2048 and 0 < height <= 2048 else None
                encoded_size = 0
            elif collecting is not None:
                if re.match(r"^;\s*thumbnail end", line):
                    try:
                        encoded = "".join(collecting)
                        raw = base64.b64decode(encoded, validate=True)
                        if (len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n" and
                                raw[12:16] == b"IHDR" and len(raw) <= 512 * 1024 and
                                struct.unpack(">II", raw[16:24]) == (width, height) and area > thumbnail_area):
                            thumbnail = "data:image/png;base64," + encoded
                            thumbnail_area = area
                    except ValueError:
                        pass
                    collecting = None
                elif encoded_size + len(line) <= 700000:
                    collecting.append(line.removeprefix(";").strip())
                    encoded_size += len(line)
                else:
                    collecting = None
    usage_text = stats.get("filament used [mm]", stats.get("filament used [g]", ""))
    usage = [float(x.strip()) for x in usage_text.split(",") if x.strip()]
    if not usage or any(not math.isfinite(x) or x < 0 for x in usage):
        raise ValueError("Missing or invalid filament usage statistics; re-slice the plate")
    types = profile_values(stats.get("filament_type") or get("filament_type"))
    colors = profile_values(stats.get("filament_colour") or get("filament_colour"))
    used = {i for i, amount in enumerate(usage) if amount > 0}
    if not used or not emitted.issubset(used):
        raise ValueError("G-code tools disagree with filament usage; check the printer macros")
    grams = []
    try:
        grams = [float(x.strip()) for x in stats.get("filament used [g]", "").split(",") if x.strip()]
        if any(not math.isfinite(x) or x < 0 for x in grams):
            grams = []
    except ValueError:
        pass
    result = []
    for tool in sorted(used):
        if tool >= len(types) or not types[tool].strip():
            raise ValueError(f"Missing project material for T{tool}")
        filament = dict(tool=tool, material=types[tool], color=colors[tool] if tool < len(colors) else "")
        if tool < len(grams):
            filament["grams"] = grams[tool]
        result.append(filament)
    if details is not None:
        total = sum(grams) if grams else None
        try:
            value = float(stats["total filament used [g]"])
            if math.isfinite(value) and value >= 0:
                total = value
        except (KeyError, ValueError):
            pass
        changes = stats.get("total filament change", "")
        details.update(thumbnail=thumbnail, grams=total,
                       time=stats.get("estimated printing time (normal mode)"),
                       swaps=int(changes) if re.fullmatch(r"\d+", changes) else
                       (0 if "total filament used [g]" in stats else None))
    return result


def connection(get):
    address = str(get("print_host") or "").strip().strip('"')
    if not address:
        raise ValueError("No Moonraker address in the send context; configure it and re-slice")
    parsed = urlsplit(address if "://" in address else "http://" + address)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use a Moonraker HTTP(S) address without embedded credentials")
    port = str(get("printhost_port") or "").strip().strip('"')
    if port and parsed.port is None:
        parsed = parsed._replace(netloc=parsed.netloc + ":" + str(int(port)))
    return Moonraker(urlunsplit(parsed).rstrip("/"), str(get("printhost_apikey") or "").strip().strip('"'))


def validate_selection(filaments, lanes, entries):
    if not isinstance(entries, dict) or set(entries) != {str(f["tool"]) for f in filaments}:
        raise ValueError("Choose one physical lane for every used filament")
    selection = {int(tool): name for tool, name in entries.items()}
    plan(lanes, selection)
    # A material mismatch is an advisory UI warning. The user may intentionally
    # map it; the plugin does not rewrite temperatures or other G-code settings.
    return selection


def mapping_html(filename, filaments, details=None):
    title = re.sub(r"\s+", " ", re.sub(r"_+", " ", re.sub(r"\.gcode$", "", filename.replace("\\", "/").rsplit("/", 1)[-1], flags=re.I))).strip()
    job = dict(details or {}, title=title, filaments=filaments)
    payload = json.dumps(job).replace("<", "\\u003c")
    return Path(__file__).with_name("mapping.html").read_text(encoding="utf-8").replace("__JOB__", payload)


def run_mapping(ctx, ui, stopped):
    """UI callbacks only queue input; network and validation stay on this worker.

    Heartbeats bound a GUI-starvation wait. ctx.cancelled() alone cannot observe
    cancellation in Orca 2.5's post-process context (ctx.print is null).
    """
    details = {}
    filaments = project_filaments(ctx.gcode_path, ctx.config_value, details)
    client = connection(ctx.config_value)
    messages = queue.Queue()
    closed = threading.Event()
    last_heartbeat = [time.monotonic()]
    def on_message(message):
        # Record liveness on the GUI callback, including while HTTP is blocking
        # the worker. Queued heartbeats must not look like a stalled GUI.
        if isinstance(message, dict) and message.get("action") in ("ready", "heartbeat"):
            last_heartbeat[0] = time.monotonic()
            if message["action"] == "heartbeat":
                return
        messages.put(message)
    window = ui.create_window(html=mapping_html(ctx.output_name, filaments, details), title="AFC tool remap",
                              width=580, height=min(820, 450 + 70 * len(filaments)), style=ui.WINDOW_MODAL,
                              on_message=on_message, on_close=closed.set)
    lanes = None
    last_error = None
    network_authorized = False
    def authorize_network():
        nonlocal network_authorized, last_error
        if network_authorized:
            return True
        try:
            client.request_network_access()
        except PermissionError:
            # Only the initial host grant has a permission recovery state.
            # Orca posts its native prompt asynchronously on this worker.
            last_error = {"kind": "network_permission",
                          "text": "Approve printer network access in Orca’s prompt, then click Refresh to continue."}
            window.post(last_error)
            return False
        network_authorized = True
        last_error = None
        return True

    def cancelled():
        return closed.is_set() or stopped.is_set() or ctx.cancelled()
    try:
        while not cancelled():
            if time.monotonic() - last_heartbeat[0] > 20:
                raise ValueError("AFC mapping UI stopped responding; send cancelled")
            if not window.is_open():
                break
            try:
                message = messages.get(timeout=0.15)
            except queue.Empty:
                continue
            if not isinstance(message, dict):
                continue
            action = message.get("action")
            try:
                if action == "skip" and not client.mapping_write_attempted:
                    return "skipped"
                if action == "refresh" and not client.mapping_write_attempted:
                    if not authorize_network():
                        continue
                    client.ensure_idle()
                    lanes = client.fetch()
                    last_error = None
                    window.post({"kind": "lanes", "lanes": lanes})
                elif action == "ready":
                    if last_error is not None:
                        window.post(last_error)
                    else:
                        if lanes is None:
                            if not authorize_network():
                                continue
                            client.ensure_idle()
                            lanes = client.fetch()
                        window.post({"kind": "lanes", "lanes": lanes})
                elif action == "apply" and lanes is not None and last_error is None:
                    selection = validate_selection(filaments, lanes, message.get("selection"))
                    client.apply(lanes, selection, cancelled)
                    if time.monotonic() - last_heartbeat[0] > 20:
                        raise ValueError("UI stopped responding during Apply; send cancelled")
                    if not cancelled():
                        return "applied"
            except Exception as error:
                consequence = ("Mapping may already have changed." if client.mapping_write_attempted
                               else "No mapping commands were sent.")
                next_step = ("Cancel this send and reopen to try again." if client.mapping_write_attempted else
                             "Refresh to retry, cancel, or skip mapping to use the printer's current assignments.")
                last_error = {"kind": "error", "mapping_write_attempted": client.mapping_write_attempted,
                              "text": str(error) + "\n" + consequence + "\n" + next_step}
                window.post(last_error)
        raise ValueError("AFC mapping closed without applying; send cancelled")
    finally:
        window.close()
        # Modal destruction is deferred. Bound this wait so a GUI joining this
        # worker cannot leave both waiting forever. Linux 2.5 is the test target.
        deadline = time.monotonic() + 2
        while window.is_open() and time.monotonic() < deadline:
            time.sleep(0.05)


try:
    import orca
except ModuleNotFoundError:
    orca = None

if orca is not None and hasattr(orca, "slicing"):
    class AFCPreSend(orca.slicing.SlicingPipelineCapabilityBase):
        def __init__(self):
            super().__init__()
            self.stopped = threading.Event()

        def get_name(self):
            return "AFC tool remap"

        def on_unload(self):
            self.stopped.set()

        def execute(self, ctx):
            if ctx.step != orca.slicing.Step.psGCodePostProcess or ctx.host != "Moonraker":
                return orca.ExecutionResult.success()
            try:
                if not re.fullmatch(r"v?2\.5\.0(?:-rc)?", ctx.orca_version):
                    raise ValueError("AFC tool remap requires OrcaSlicer 2.5.0")
                if not ctx.output_name.lower().endswith(".gcode"):
                    raise ValueError("AFC tool remap supports only .gcode sends")
                outcome = run_mapping(ctx, orca.host.ui, self.stopped)
                return orca.ExecutionResult.success(
                    "Mapping skipped; continuing Orca upload" if outcome == "skipped" else
                    "AFC mapping verified; continuing Orca upload")
            except Exception as error:
                return orca.ExecutionResult.failure(orca.PluginResult.FatalError, str(error))

    @orca.plugin
    class AFCPreSendPackage(orca.base):
        def register_capabilities(self):
            orca.register_capability(AFCPreSend)
