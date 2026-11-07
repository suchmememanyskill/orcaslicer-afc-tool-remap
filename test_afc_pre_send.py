import itertools
import types
import tempfile
import io
import json
import hashlib
import zipfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import ProxyHandler
from urllib.error import HTTPError
import sys
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import afc_pre_send as afc


def fixture():
    db = {"result": {"value": {
        "unrelated-key-" + str(i): {"lane": str(i), "material": "PLA", "color": "#112233"}
        for i in range(3)}}}
    status = {"units": ["BoxTurtle Turtle_1"], "lanes": ["laneA", "laneB", "laneC"],
              "maps": ["T2", "T0", "T1"]}
    obj = "AFC_BoxTurtle Turtle_1"
    units = {obj: {"result": {"status": {obj: {"lanes": ["laneA", "laneB", "laneC"]}}}}}
    lane_response = {"result": {"status": {
        "AFC_stepper " + name: {"map": [tool]} for name, tool in
        zip(status["lanes"], ["T2", "T0", "T1"])}}}
    return db, {"result": {"status": {"AFC": status}}}, units, lane_response


class Tests(unittest.TestCase):
    def test_join_and_non_four_lane_unit(self):
        lanes = afc.parse_lanes(*fixture())
        self.assertEqual([(x["name"], x["tool"], x["position"]) for x in lanes],
                         [("laneA", 2, 0), ("laneB", 0, 1), ("laneC", 1, 2)])

    def test_global_maps_registry_is_not_paired_with_physical_lanes(self):
        database, response, units, lane_response = fixture()
        # Global map keys may be in another order and include virtual tool names.
        response["result"]["status"]["AFC"]["maps"] = ["T0", "T1", "T2", "T99"]
        for tool in range(3):
            database["result"]["value"]["unrelated-key-"+str(tool)]["color"] = "#"+str(tool)*6
        lanes = afc.parse_lanes(database, response, units, lane_response)
        self.assertEqual([(lane["name"],lane["tool"],lane["color"]) for lane in lanes],
                         [("laneA",2,"#222222"),("laneB",0,"#000000"),("laneC",1,"#111111")])

    def test_multiple_mapping_mode_is_explicitly_unsupported(self):
        database, response, units, lane_response = fixture()
        response["result"]["status"]["AFC"]["multiple_tool_mapping"] = True
        with self.assertRaisesRegex(ValueError, "Multiple tool mapping is enabled"):
            afc.parse_lanes(database,response,units,lane_response)

    def test_empty_lane_null_metadata(self):
        database, response, units, lane_response = fixture()
        record = database["result"]["value"]["unrelated-key-2"]
        record.update(material=None, color=None)
        lanes = afc.parse_lanes(database, response, units, lane_response)
        self.assertEqual(lanes[0]["name"], "laneA")
        self.assertEqual((lanes[0]["material"], lanes[0]["color"]), ("", ""))
        with self.assertRaisesRegex(ValueError, "no material"):
            afc.plan(lanes, {0: "laneA"})
        # The other populated physical lanes remain usable.
        self.assertEqual(afc.plan(lanes, {0: "laneB"})[0], [])
        record["color"] = 123
        with self.assertRaisesRegex(ValueError, "color for physical lane laneA"):
            afc.parse_lanes(database, response, units, lane_response)

    def test_remaining_weight_is_optional_and_joined_by_current_tool(self):
        database, response, units, lane_response = fixture()
        records = database["result"]["value"]
        records["unrelated-key-2"]["weight"] = 984.67
        lanes = afc.parse_lanes(database, response, units, lane_response)
        self.assertEqual(lanes[0]["name"], "laneA")
        self.assertEqual(lanes[0]["remaining_grams"], 984.67)
        self.assertIsNone(lanes[1]["remaining_grams"])
        for invalid in (None, -1, float("nan"), True, "unknown"):
            records["unrelated-key-2"]["weight"] = invalid
            self.assertIsNone(afc.parse_lanes(database, response, units, lane_response)[0]["remaining_grams"])
        records["unrelated-key-2"]["weight"] = 0
        self.assertEqual(afc.parse_lanes(database, response, units, lane_response)[0]["remaining_grams"], 0)

    def test_wheel_bundles_template_and_renders_after_extraction(self):
        from build_plugin import build
        with tempfile.TemporaryDirectory() as directory:
            wheel = build(directory)
            with zipfile.ZipFile(wheel) as archive:
                names = archive.namelist()
                record_name = next(n for n in names if n.endswith("/RECORD"))
                for name, digest, size in afc.csv.reader(archive.read(record_name).decode().splitlines()):
                    if digest:
                        content = archive.read(name)
                        actual = afc.base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
                        self.assertEqual(digest, "sha256="+actual)
                        self.assertEqual(int(size), len(content))
                archive.extractall(Path(directory)/"extracted")
            entry = Path(directory)/"extracted/afc_pre_send/__init__.py"
            spec = importlib.util.spec_from_file_location("wheel_plugin", entry)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            page = module.mapping_html("Cube_PLA.gcode", [])
            self.assertIn('"title": "Cube PLA"', page)
            self.assertNotIn("__JOB__", page)
            self.assertEqual(entry.with_name("mapping.html").read_text(), Path(afc.__file__).with_name("mapping.html").read_text())

    def test_all_partial_and_full_permutations(self):
        lanes = afc.parse_lanes(*fixture())
        for count in (1, 2, 3):
            for tools in itertools.combinations(range(3), count):
                for names in itertools.permutations([x["name"] for x in lanes], count):
                    desired = dict(zip(tools, names))
                    commands, expected = afc.plan(lanes, desired)
                    actual = {x["tool"]: x["name"] for x in lanes}
                    for command in commands:
                        match = afc.re.fullmatch(r"SET_MAP LANE=(\w+) MAP=T(\d+)", command)
                        name, target = match[1], int(match[2])
                        other = next(t for t, n in actual.items() if n == name)
                        actual[target], actual[other] = actual[other], actual[target]
                    self.assertEqual(actual, expected)
                    self.assertTrue(all(actual[t] == n for t, n in desired.items()))

    def test_reject_invalid_selection(self):
        lanes = afc.parse_lanes(*fixture())
        for selection in ({0: "laneA", 1: "laneA"}, {4: "laneA"}, {0: "missing"}):
            with self.assertRaises(ValueError):
                afc.plan(lanes, selection)
        lanes[0]["material"] = ""
        with self.assertRaises(ValueError):
            afc.plan(lanes, {0: "laneA"})

    def test_fetch_encoded_units_and_apply_readback(self):
        db, response, units, lane_response = fixture()
        calls = []
        status = response["result"]["status"]["AFC"]
        def request(path, payload=None):
            calls.append((path, payload))
            if path == "/server/database/item?namespace=lane_data":
                return db
            if path == "/printer/objects/query?print_stats":
                return {"result": {"status": {"print_stats": {"state": "standby"}}}}
            if path == "/printer/objects/query?AFC":
                return response
            if path == "/printer/objects/query?AFC_BoxTurtle%20Turtle_1":
                return next(iter(units.values()))
            if path == "/printer/objects/query?AFC_stepper%20laneA&AFC_stepper%20laneB&AFC_stepper%20laneC":
                return lane_response
            if path == "/printer/gcode/script":
                self.assertEqual(payload, {"script": "SET_MAP LANE=laneA MAP=T0"})
                # Registry order remains unchanged even though the physical maps swap.
                lane_response["result"]["status"]["AFC_stepper laneA"]["map"] = ["T0"]
                lane_response["result"]["status"]["AFC_stepper laneB"]["map"] = ["T2"]
                return {"result": "ok"}
            self.fail(path)
        client = afc.Moonraker("unused.invalid")
        with patch.object(client, "request", side_effect=request):
            snapshot = client.fetch()
            client.apply(snapshot, {0: "laneA"})
        self.assertEqual(sum(p == "/printer/gcode/script" for p, _ in calls), 1)

    def load_plugin(self):
        fake = types.SimpleNamespace(
            slicing=types.SimpleNamespace(SlicingPipelineCapabilityBase=object,
                                          Step=types.SimpleNamespace(psGCodePostProcess=99)),
            base=object, plugin=lambda cls: cls,
            register_capability=Mock(),
            host=types.SimpleNamespace(ui=object()),
            PluginResult=types.SimpleNamespace(FatalError="fatal"),
            ExecutionResult=types.SimpleNamespace(success=lambda *args: "success",
                failure=lambda status, message: (status, message)))
        spec = importlib.util.spec_from_file_location("plugin_under_test", Path(afc.__file__))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, orca=fake):
            spec.loader.exec_module(module)
        return module

    def test_hook_filters_success_and_cancel(self):
        module = self.load_plugin()
        capability = module.AFCPreSend()
        for step, host in ((0, "Moonraker"), (99, "File"), (99, "OctoPrint")):
            self.assertEqual(capability.execute(types.SimpleNamespace(step=step, host=host)), "success")
        ctx = types.SimpleNamespace(step=99, host="Moonraker", output_name="test.gcode", orca_version="2.5.0")
        with patch.object(module, "run_mapping") as run:
            self.assertEqual(capability.execute(ctx), "success")
            run.assert_called_once()
        with patch.object(module, "run_mapping", side_effect=ValueError("closed")):
            self.assertEqual(capability.execute(ctx), ("fatal", "closed"))
        ctx.output_name = "test.gcode.3mf"
        self.assertEqual(capability.execute(ctx)[0], "fatal")

    def test_gcode_usage_and_connection(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".gcode") as stream:
            stream.write("T2 PURGE_LENGTH=10\n; filament used [mm] = 0, 0, 15\n")
            stream.flush()
            get = {"filament_type": 'PLA;ABS;"PETG"', "filament_colour": '#000000;#111111;#123456'}.get
            self.assertEqual(afc.project_filaments(stream.name, get),
                             [{"tool": 2, "material": "PETG", "color": "#123456"}])
        client = afc.connection({"print_host": "http://[::1]/moonraker", "printhost_port": "7125", "printhost_apikey": "test"}.get)
        self.assertEqual(client.address, "http://[::1]:7125/moonraker")
        self.assertEqual(client.api_key, "test")

    def test_job_metadata_and_thumbnail(self):
        # Tiny PNG header is sufficient for the parser's signature/dimension check.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + afc.struct.pack(">II", 1, 1)
        encoded = afc.base64.b64encode(png).decode()
        text = ("; thumbnail begin 1x1 100\n; " + encoded + "\n; thumbnail end\n"
                "; filament used [mm] = 0, 50\n; filament used [g] = 0, 1.25\n"
                "; filament_type = PLA;PETG\n; filament_colour = #111111;#222222\n"
                "; total filament used [g] = 1.25\n; total filament change = 4\n"
                "; estimated printing time (normal mode) = 12m 30s\n")
        with tempfile.NamedTemporaryFile(mode="w") as stream:
            stream.write(text); stream.flush()
            details = {}
            filaments = afc.project_filaments(stream.name, lambda key: None, details)
        self.assertEqual(details, {"thumbnail": "data:image/png;base64,"+encoded,
                                  "grams": 1.25, "swaps": 4, "time": "12m 30s"})
        self.assertEqual(filaments[0]["tool"], 1)
        self.assertEqual(filaments[0]["grams"], 1.25)
        page = afc.mapping_html('Cube_PLA.gcode', filaments, details)
        self.assertIn('"title": "Cube PLA"', page)
        self.assertIn('var(--orca-bg', page)
        self.assertIn('Skip mapping', page)
        injected = afc.mapping_html('</script>_test.gcode', filaments)
        self.assertNotIn('"title": "</script>', injected)

    def test_skip_continues_without_printer_commands(self):
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        def create_window(**kwargs):
            kwargs["on_message"]({"action": "skip"})
            return window
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        ctx = types.SimpleNamespace(output_name="part.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        client = Mock(mapping_write_attempted=False)
        with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
            self.assertEqual(afc.run_mapping(ctx, ui, threading.Event()), "skipped")
        client.ensure_idle.assert_not_called()
        client.fetch.assert_not_called()
        client.apply.assert_not_called()
        self.assertFalse(window.is_open())

    def test_zero_swaps_in_single_filament_statistics(self):
        text = "; filament used [mm] = 10\n; filament_type = PLA\n; total filament used [g] = 0.3\n"
        with tempfile.NamedTemporaryFile(mode="w") as stream:
            stream.write(text); stream.flush()
            details = {}
            afc.project_filaments(stream.name, lambda key: None, details)
        self.assertEqual(details["swaps"], 0)

    def test_skip_refused_after_write_attempt(self):
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        def create_window(**kwargs):
            window.post = lambda message: kwargs["on_close"]()
            kwargs["on_message"]({"action": "skip"})
            kwargs["on_message"]({"action": "ready"})
            return window
        client = Mock(mapping_write_attempted=True)
        client.fetch.side_effect = ValueError("Read failure")
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
            with self.assertRaisesRegex(ValueError, "closed"):
                afc.run_mapping(ctx, ui, threading.Event())
        client.ensure_idle.assert_called_once()
        client.apply.assert_not_called()

    def test_live_heartbeats_during_slow_apply(self):
        clock = [0]
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        def create_window(**kwargs):
            window.message = kwargs["on_message"]
            window.post = lambda message: window.message({"action": "apply", "selection": {"0": "laneA"}})
            window.message({"action": "ready"})
            return window
        client = Mock(mapping_write_attempted=False)
        client.fetch.return_value = afc.parse_lanes(*fixture())
        def apply(*args):
            clock[0] = 25
            window.message({"action": "heartbeat"})
        client.apply.side_effect = apply
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        with patch.object(afc, "project_filaments", return_value=[{"tool": 0, "material": "PLA", "color": ""}]), patch.object(afc, "connection", return_value=client), patch.object(afc.time, "monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(afc.run_mapping(ctx, ui, threading.Event()), "applied")

    def test_reloaded_page_receives_cached_lanes(self):
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        replies = []
        def create_window(**kwargs):
            def post(message):
                replies.append(message)
                if len(replies) == 2:
                    kwargs["on_close"]()
            window.post = post
            kwargs["on_message"]({"action": "ready"})
            kwargs["on_message"]({"action": "ready"})
            return window
        client = Mock(mapping_write_attempted=False)
        client.fetch.return_value = afc.parse_lanes(*fixture())
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
            with self.assertRaisesRegex(ValueError, "closed"):
                afc.run_mapping(ctx, ui, threading.Event())
        self.assertEqual([reply["kind"] for reply in replies], ["lanes", "lanes"])
        client.fetch.assert_called_once()

    def test_refresh_replaces_snapshot_before_apply(self):
        initial = afc.parse_lanes(*fixture())
        fresh = [dict(lane, color="#fedcba") for lane in initial]
        client = Mock(mapping_write_attempted=False)
        client.fetch.side_effect = [initial, fresh]
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        replies = []
        def create_window(**kwargs):
            def post(message):
                replies.append(message)
                kwargs["on_message"]({"action": "refresh"} if len(replies)==1 else
                                     {"action": "apply", "selection": {"0": "laneA"}})
            window.post = post
            kwargs["on_message"]({"action": "ready"})
            return window
        ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        with patch.object(afc, "project_filaments", return_value=[{"tool": 0, "material": "PLA", "color": ""}]), patch.object(afc, "connection", return_value=client):
            self.assertEqual(afc.run_mapping(ctx, ui, threading.Event()), "applied")
        self.assertEqual(client.fetch.call_count, 2)
        self.assertEqual(replies[1]["lanes"], fresh)
        client.apply.assert_called_once()
        self.assertEqual(client.apply.call_args.args[:2], (fresh, {0:"laneA"}))

    def test_refresh_can_recover_from_read_error_without_writes(self):
        client = Mock(mapping_write_attempted=False)
        client.fetch.side_effect = [ValueError("temporary read failure"), afc.parse_lanes(*fixture())]
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        kinds = []
        def create_window(**kwargs):
            def post(message):
                kinds.append(message["kind"])
                if message["kind"] == "error":
                    kwargs["on_message"]({"action": "refresh"})
                else:
                    kwargs["on_close"]()
            window.post = post
            kwargs["on_message"]({"action": "ready"})
            return window
        ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
            with self.assertRaisesRegex(ValueError, "closed"):
                afc.run_mapping(ctx, ui, threading.Event())
        self.assertEqual(kinds, ["error", "lanes"])
        self.assertEqual(client.fetch.call_count, 2)
        client.apply.assert_not_called()

    def test_refresh_refused_after_mapping_write_attempt(self):
        client = Mock(mapping_write_attempted=True)
        client.fetch.side_effect = ValueError("read error")
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        def create_window(**kwargs):
            window.post = lambda message: kwargs["on_close"]()
            kwargs["on_message"]({"action": "refresh"})
            kwargs["on_message"]({"action": "ready"})
            return window
        ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
        with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
            with self.assertRaisesRegex(ValueError, "closed"):
                afc.run_mapping(ctx, ui, threading.Event())
        client.fetch.assert_called_once()
        client.apply.assert_not_called()

    def test_material_mismatch_is_advisory_but_missing_tools_block(self):
        lanes = afc.parse_lanes(*fixture())
        filaments = [{"tool": 0, "material": "PETG"}]
        self.assertEqual(afc.validate_selection(filaments, lanes, {"0": "laneA"}), {0: "laneA"})
        with self.assertRaises(ValueError):
            afc.validate_selection(filaments, lanes, {})

    def test_busy_stale_cancelled_and_unverified(self):
        client = afc.Moonraker("unused.invalid")
        lanes = afc.parse_lanes(*fixture())
        with patch.object(client, "query", return_value={"result": {"status": {"print_stats": {"state": "printing"}}}}):
            with self.assertRaisesRegex(ValueError, "idle"):
                client.apply(lanes, {0: "laneA"})
        with patch.object(client, "ensure_idle"), patch.object(client, "fetch", return_value=[]):
            with self.assertRaisesRegex(ValueError, "changed"):
                client.apply(lanes, {0: "laneA"})
        with patch.object(client, "ensure_idle"), patch.object(client, "fetch", return_value=lanes), patch.object(client, "request") as request:
            with self.assertRaisesRegex(ValueError, "cancelled"):
                client.apply(lanes, {0: "laneA"}, lambda: True)
            request.assert_not_called()
            with patch.object(client, "query_lane_maps", return_value=fixture()[3]), patch.object(afc.time, "sleep"):
                with self.assertRaisesRegex(ValueError, "verification"):
                    client.apply(lanes, {0: "laneA"})

    def test_audit_permission_read_retries_once_after_callback(self):
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        for wrapped in (False, True):
            client = afc.Moonraker("http://unused.invalid")
            client.retry_permission = Mock(return_value=True)
            failure = afc.URLError(denial) if wrapped else denial
            with patch.object(client.opener, "open", side_effect=[failure, io.BytesIO(b'{"result":"ok"}')]) as request:
                self.assertEqual(client.request("/printer/objects/query?AFC"), {"result":"ok"})
            self.assertEqual(request.call_count, 2)
            client.retry_permission.assert_called_once()

    def test_permission_retry_is_bounded(self):
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        client = afc.Moonraker("http://unused.invalid")
        client.retry_permission = Mock(return_value=True)
        with patch.object(client.opener, "open", side_effect=denial) as request:
            with self.assertRaises(PermissionError):
                client.request("/printer/objects/query?AFC")
        self.assertEqual(request.call_count, 2)
        client.retry_permission.assert_called_once()

    def test_permission_retry_never_resubmits_mapping_post(self):
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        client = afc.Moonraker("http://unused.invalid")
        client.retry_permission = Mock(return_value=True)
        with patch.object(client.opener, "open", side_effect=denial) as request:
            with self.assertRaises(PermissionError):
                client.request("/printer/gcode/script", {"script":"SET_MAP LANE=laneA MAP=T0"})
        request.assert_called_once()
        client.retry_permission.assert_not_called()

    def test_unrelated_errors_and_cancelled_permission_wait_are_not_retried(self):
        client = afc.Moonraker("http://unused.invalid")
        for failure in (afc.URLError("timed out"), PermissionError("filesystem permission denied")):
            client.retry_permission = Mock(return_value=True)
            with patch.object(client.opener, "open", side_effect=failure) as request:
                with self.assertRaises(type(failure)):
                    client.request("/printer/objects/query?AFC")
            request.assert_called_once()
            client.retry_permission.assert_not_called()
        client.retry_permission = Mock(return_value=False)
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        with patch.object(client.opener, "open", side_effect=denial) as request:
            with self.assertRaises(PermissionError):
                client.request("/printer/objects/query?AFC")
        request.assert_called_once()

    def test_permission_prompt_retry_and_skip_in_same_dialog(self):
        for skip in (False, True):
            client = Mock(mapping_write_attempted=False)
            client.fetch.return_value = afc.parse_lanes(*fixture())
            window = types.SimpleNamespace(is_open=lambda: True)
            window.close = lambda: setattr(window, "is_open", lambda: False)
            kinds = []
            def create_window(**kwargs):
                def post(message):
                    kinds.append(message["kind"])
                    if message["kind"] == "permission":
                        kwargs["on_message"]({"action":"skip"} if skip else
                                             {"action":"permission_retry", "token":message["token"]})
                    elif message["kind"] == "lanes":
                        kwargs["on_close"]()
                window.post = post
                kwargs["on_message"]({"action":"ready"})
                return window
            def ensure_idle():
                if not client.retry_permission():
                    raise PermissionError("permission wait ended")
            client.ensure_idle.side_effect = ensure_idle
            ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                        cancelled=lambda: False)
            ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
            with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
                if skip:
                    self.assertEqual(afc.run_mapping(ctx,ui,threading.Event()), "skipped")
                    client.fetch.assert_not_called()
                else:
                    with self.assertRaisesRegex(ValueError,"closed"):
                        afc.run_mapping(ctx,ui,threading.Event())
                    self.assertEqual(kinds,["permission","lanes"])
            client.apply.assert_not_called()
            self.assertIsNone(client.retry_permission)

    def test_permission_wait_ignores_stale_and_unscoped_retry_events(self):
        client = Mock(mapping_write_attempted=False)
        window = types.SimpleNamespace(is_open=lambda: True)
        window.close = lambda: setattr(window, "is_open", lambda: False)
        def create_window(**kwargs):
            def post(message):
                if message["kind"] == "permission":
                    kwargs["on_message"]({"action":"permission_retry","token":message["token"]-1})
                    kwargs["on_message"]({"action":"refresh"})
                    kwargs["on_message"]({"action":"skip"})
                else:
                    kwargs["on_close"]()
            window.post = post
            kwargs["on_message"]({"action":"ready"})
            return window
        def ensure_idle():
            if not client.retry_permission():
                raise PermissionError("permission wait ended")
        client.ensure_idle.side_effect = ensure_idle
        ctx = types.SimpleNamespace(output_name="test.gcode",gcode_path="unused",config_value=lambda key:None,
                                    cancelled=lambda:False)
        ui = types.SimpleNamespace(WINDOW_MODAL=1,create_window=create_window)
        with patch.object(afc,"project_filaments",return_value=[]),patch.object(afc,"connection",return_value=client):
            self.assertEqual(afc.run_mapping(ctx,ui,threading.Event()),"skipped")
        client.fetch.assert_not_called()
        client.apply.assert_not_called()

    def test_mapping_post_audit_denial_restores_no_write_state(self):
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        lanes = afc.parse_lanes(*fixture())
        for wrapped in (False, True):
            client = afc.Moonraker("http://unused.invalid")
            failure = afc.URLError(denial) if wrapped else denial
            with patch.object(client,"ensure_idle"), patch.object(client,"fetch",return_value=lanes), patch.object(client,"request",side_effect=failure) as request:
                with self.assertRaisesRegex(ValueError,"before it was sent"):
                    client.apply(lanes,{0:"laneA"})
            self.assertFalse(client.mapping_write_attempted)
            request.assert_called_once()

    def test_uncertain_mapping_failure_and_prior_write_stay_uncertain(self):
        lanes = afc.parse_lanes(*fixture())
        late = PermissionError('Plugin attempted audited operation "http.client.send" without permission: audit permission required')
        early = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        for failure, prior in ((afc.URLError("timed out"),False),(late,False),(early,True)):
            client = afc.Moonraker("http://unused.invalid")
            client.mapping_write_attempted = prior
            with patch.object(client,"ensure_idle"), patch.object(client,"fetch",return_value=lanes), patch.object(client,"request",side_effect=failure) as request:
                with self.assertRaises(ValueError if prior else type(failure)):
                    client.apply(lanes,{0:"laneA"})
            self.assertTrue(client.mapping_write_attempted)
            request.assert_called_once()

    def test_permission_blocked_post_can_recover_with_refresh_and_manual_apply(self):
        lanes = afc.parse_lanes(*fixture())
        readback = fixture()[3]
        readback["result"]["status"]["AFC_stepper laneA"]["map"] = ["T0"]
        readback["result"]["status"]["AFC_stepper laneB"]["map"] = ["T2"]
        denial = PermissionError('Plugin attempted audited operation "urllib.Request" without permission: audit permission required')
        client = afc.Moonraker("http://unused.invalid")
        window = types.SimpleNamespace(is_open=lambda:True)
        window.close = lambda:setattr(window,"is_open",lambda:False)
        kinds = []
        def create_window(**kwargs):
            def post(message):
                kinds.append(message["kind"])
                if message["kind"] == "error":
                    self.assertFalse(message["mapping_write_attempted"])
                    self.assertIn("No mapping commands were sent",message["text"])
                    kwargs["on_message"]({"action":"refresh"})
                else:
                    kwargs["on_message"]({"action":"apply","selection":{"0":"laneA"}})
            window.post = post
            kwargs["on_message"]({"action":"ready"})
            return window
        ctx = types.SimpleNamespace(output_name="test.gcode",gcode_path="unused",config_value=lambda key:None,
                                    cancelled=lambda:False)
        ui = types.SimpleNamespace(WINDOW_MODAL=1,create_window=create_window)
        with patch.object(afc,"project_filaments",return_value=[{"tool":0,"material":"PLA","color":""}]), patch.object(afc,"connection",return_value=client), patch.object(client,"ensure_idle"), patch.object(client,"fetch",return_value=lanes), patch.object(client,"request",side_effect=[denial,{"result":"ok"}]) as request, patch.object(client,"query_lane_maps",return_value=readback) as verify:
            self.assertEqual(afc.run_mapping(ctx,ui,threading.Event()),"applied")
        self.assertEqual(kinds,["lanes","error","lanes"])
        self.assertEqual(request.call_count,2)  # blocked attempt, then explicit second Apply
        verify.assert_called_once()

    def test_post_redirect_is_not_followed(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length",0)))
                calls.append(("POST",self.path))
                self.send_response(302)
                self.send_header("Location","/redirected")
                self.end_headers()
            def do_GET(self):
                calls.append(("GET",self.path))
                self.send_response(200);self.end_headers();self.wfile.write(b'{"result":"ok"}')
            def log_message(self,*args): pass
        server = HTTPServer(("127.0.0.1",0),Handler)
        thread = threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01),daemon=True)
        thread.start()
        try:
            client = afc.Moonraker("http://127.0.0.1:"+str(server.server_port))
            client.opener = afc.build_opener(afc.NoRedirect(),ProxyHandler({}))
            with self.assertRaises(HTTPError) as result:
                client.request("/mapping",{"script":"mock only"})
            result.exception.close()
            self.assertEqual(calls,[("POST","/mapping")])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=1)

    def test_http_request_contract(self):
        client = afc.Moonraker("http://unused.invalid:7125", "test-key")
        with patch.object(client.opener, "open", return_value=io.BytesIO(b'{"result":"ok"}')) as open_url:
            self.assertEqual(client.request("/printer/gcode/script", {"script": "SET_MAP LANE=laneA MAP=T0"}), {"result": "ok"})
            request = open_url.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("X-api-key"), "test-key")
            self.assertEqual(json.loads(request.data), {"script": "SET_MAP LANE=laneA MAP=T0"})
            self.assertEqual(open_url.call_args.kwargs["timeout"], 5)

    def test_error_message_reports_whether_write_was_attempted(self):
        for attempted in (False, True):
            messages = []
            window = types.SimpleNamespace(is_open=lambda: True, close=lambda: None)
            def create_window(**kwargs):
                def post(message):
                    messages.append(message)
                    kwargs["on_close"]()
                window.post = post
                window.close = lambda: setattr(window, "is_open", lambda: False)
                kwargs["on_message"]({"action": "ready"})
                return window
            ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
            ctx = types.SimpleNamespace(output_name="test.gcode", gcode_path="unused", config_value=lambda key: None,
                                        cancelled=lambda: False)
            client = Mock(mapping_write_attempted=attempted)
            client.fetch.side_effect = ValueError("Example failure")
            with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection", return_value=client):
                with self.assertRaisesRegex(ValueError, "closed"):
                    afc.run_mapping(ctx, ui, threading.Event())
            text = messages[0]["text"]
            self.assertIn("Mapping may already have changed." if attempted else "No mapping commands were sent.", text)
            self.assertNotIn("No mapping commands were sent." if attempted else "Mapping may already have changed.", text)

    def test_ui_starvation_has_bounded_exit(self):
        window = types.SimpleNamespace(is_open=lambda: True, close=lambda: None)
        ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=lambda **kwargs: window)
        ctx = types.SimpleNamespace(output_name="part.gcode", gcode_path="unused", config_value=lambda key: None,
                                    cancelled=lambda: False)
        # Simulate a GUI which never services the queued window creation.
        times = iter([0, 21, 21, 24])
        with patch.object(afc, "project_filaments", return_value=[]), patch.object(afc, "connection"), patch.object(afc.time, "monotonic", side_effect=lambda: next(times)):
            with self.assertRaisesRegex(ValueError, "stopped responding"):
                afc.run_mapping(ctx, ui, threading.Event())

    def test_mapping_ui_happy_flow_and_close(self):
        # Fake only the UI bridge and HTTP client, exercising the actual event loop.
        ctx = types.SimpleNamespace(output_name="part.gcode", gcode_path="working-copy",
                                    config_value=lambda key: None, cancelled=lambda: False)
        lanes = afc.parse_lanes(*fixture())
        for apply in (True, False):
            class Window:
                opened = True
                def is_open(self): return self.opened
                def close(self): self.opened = False
                def post(self, message):
                    if message["kind"] == "lanes":
                        if apply: self.message({"action": "apply", "selection": {"0": "laneA"}})
                        else: self.closed()
            window = Window()
            def create_window(**kwargs):
                window.message, window.closed = kwargs["on_message"], kwargs["on_close"]
                kwargs["on_message"]({"action": "ready"})
                return window
            ui = types.SimpleNamespace(WINDOW_MODAL=1, create_window=create_window)
            with patch.object(afc, "project_filaments", return_value=[{"tool": 0, "material": "PLA", "color": "#123456"}]), patch.object(afc, "connection") as connect:
                connect.return_value.fetch.return_value = lanes
                if apply:
                    afc.run_mapping(ctx, ui, threading.Event())
                    connect.return_value.apply.assert_called_once()
                    self.assertEqual(connect.return_value.apply.call_args.args[1], {0: "laneA"})
                else:
                    with self.assertRaisesRegex(ValueError, "closed"):
                        afc.run_mapping(ctx, ui, threading.Event())
                    connect.return_value.apply.assert_not_called()
                self.assertFalse(window.opened)


if __name__ == "__main__":
    unittest.main()
