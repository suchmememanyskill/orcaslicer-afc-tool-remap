# AFC tool remap — experimental OrcaSlicer 2.5 plugin

Python plugin with a separate `mapping.html` UI template. The wheel bundles both files.
No Orca source modifications, extra Python dependencies,
uploader, print-start call, or G-code rewriting. It uses the ordinary configured
Moonraker PrintHost and Orca's existing Upload / Upload & Print flow.

## Install

1. Use an OrcaSlicer build with the Python slicing-pipeline plugin API (developed
   against OrcaSlicer 2.5.0).
2. Build the wheel using the command below, then open **Plugins > Install local
   plugin** and select `afc_tool_remap-0.3.13-py3-none-any.whl`.
   Installing the `.py` alone does not copy the HTML template.
3. The plugin uses Orca's ordinary runtime permission checks and saved grants.
   It declares no fixed printer addresses and requires no setup/reload workflow.
   In 2.5, a denied background request queues a prompt and fails that attempt;
   accepting it permits a subsequent attempt. An approval for a full endpoint
   can remain limited to that URL. A host-wide grant in Plugins > Permissions
   avoids repeated endpoint prompts. The plugin reads only `ctx.gcode_path`,
   normally a temporary file, but Orca may prompt for each new working path.
   Full access is an existing Orca option for local testing, not a plugin requirement.
4. Add **AFC tool remap** under **Process > Others > Slicing Pipeline Plugin**.
   Save the process preset. Keep your existing Moonraker address/key and AFC-aware
   start/change-filament macros, then re-slice.

Build an installable wheel from the source directory with:

```sh
python3 build_plugin.py --output-dir /path/to/output
```

The builder uses only Python's standard library. `afc_pre_send.py` holds plugin
logic; `mapping.html` contains the HTML, CSS and JavaScript. The installed development
copy also keeps these files side by side for editing.

No printer address or API key is stored in this plugin. They come from
`ctx.config_value()` for the send job, as in SnapSend. An empty address fails
explicitly; it does not guess a printer or scrape profile files.

To remove: remove AFC tool remap from the process preset, then uninstall the local
package through Plugins. Restarting without it restores the ordinary send flow.

## Use

Click Print, then choose Upload or Upload & Print normally. For a Moonraker
`.gcode` send, the plugin opens its mapping window. Review the outgoing T numbers
and project material/color, then choose a physical unit/lane for each used filament.
The dialog uses Orca's live theme colors/font and button styling. It shows the embedded PNG preview, a readable filename, estimated time, total grams and filament swap count. The default is each tool's current lane. Different tools require different lanes,
and a material mismatch is shown as an amber warning but does not block Apply.
Each row shows a check for a valid selection, a warning for a material mismatch, or an X for an empty, missing or duplicate lane. Empty, missing and duplicate selections keep Apply disabled; material warnings are advisory. The printer must be idle.

**Skip mapping** continues Orca's upload flow using the printer's current mapping, without sending SET_MAP. This is also available after a read failure, but is disabled once a mapping write has been attempted. It does not change the Upload versus Upload & Print choice.

**Refresh** (the circular-arrow icon beside Physical lane) rereads AFC lane assignments, materials, colors and remaining weights. It preserves selections by physical lane name and updates the snapshot used by Apply. It sends no mapping commands. It is disabled while loading/applying and after any mapping write attempt; a failed read can be retried with Refresh.

**Apply & Continue** refetches the lane snapshot, rejects changes since review,
applies only necessary SET_MAP swaps, and verifies the complete mapping. Orca then
continues its own upload and optional print start. Mapping changes persist on the
printer, even if an eventual upload fails. Selecting the same assignments issues
no SET_MAP commands. Closing the window or Cancel send returns a fatal plugin
result and aborts upload. After a failed mapping write, cancel and reopen the send; it is never automatically retried because some swaps may already have persisted. Failures before a write can be retried by refreshing.

Disk exports and other PrintHosts are no-ops. Archives are unsupported. Usage comes
from Orca's per-filament G-code statistics, retaining zero-based project indices,
with emitted T commands checked for consistency. Physical nozzle indices are never
used as filament indices. One physical extruder and one-to-one mapping are supported. When AFC reports `multiple_tool_mapping` enabled, the plugin refuses mapping explicitly; support for that mode is deferred. AFC's lanes array lists physical identities. Each `AFC_stepper <lane>` object's `map` list supplies its actual tool assignment; the global `AFC.maps` is only a registry and is never paired with the lane array. Database material/color records join to those actual tool assignments, not to database keys.
Unit membership comes from URL-encoded AFC unit objects, with no four-lane assumption. The dropdown displays remaining grams from the optional `weight` in lane_data; when absent or invalid, it omits the weight. No Spoolman requests are needed, and unit names are not shown in the menu.
The existing printer macros remain responsible for loading/changing filament.

## Cancellation limitation and reproduction

This is an experimental interactive worker hook, authorized for functional testing.
Orca's `PluginHostUi.hpp` warns against UI calls from slicing workers. In the
inspected 2.5 source, GUI stop/shutdown can wait for the export worker, while this
hook's `ctx.cancelled()` always returns false because `ctx.print` is null.

SnapSend 0.10.32 was inspected without execution. It uses a modal HTML window,
worker event waits, context profile reads and fatal results to abort upload. This
implementation follows that basic flow, while keeping network operations on the
existing export worker and GUI callbacks limited to queueing messages.

A one-second UI heartbeat bounds a silent-GUI wait to about 20 seconds, and the
modal close wait is bounded to two seconds. Individual HTTP requests time out
at five seconds. These bounds reduce indefinite waits; they do not implement
host-level cancellation. A UI stall can still delay cancellation, and slow
network/permission dialogs may cause a send to abort. The 20-second heartbeat
limit is checked between network operations. No deadlock reproduction or real
in-app GUI test has been completed.

For initial printer testing, choose **Upload** rather than Upload & Print to avoid
starting a print. Closing before Apply should issue no SET_MAP commands or upload.
Apply can mutate mapping; only test that on the intended idle printer. To investigate
GUI stop/shutdown behavior without hardware, use an isolated test profile and a
mock Moonraker service; the offline tests already exercise Apply and Cancel with
mocked transport and UI. CC1 was queried read-only to verify its lane data and unit layout. Its empty fourth lane reports JSON null material, now treated as empty. No mapping writes, uploads, or print starts were performed during development.

## Verification

From the repository root:

```sh
python3 -B -m unittest discover -s . -v
```

Thirty-six tests cover variable unit sizes, database joins, all partial/full permutations
of three lanes, invalid/missing/material-mismatched selections, G-code usage,
connection settings, HTTP auth/payload, stale/busy/cancelled/unverified mapping,
hook filters, UI Apply/Cancel, empty-lane null metadata, read/write error reporting, thumbnail/print statistics parsing, Skip without printer commands, refusal to skip after a write, zero swaps, live heartbeats during slow Apply, page reloads, optional remaining weight, wheel contents/template loading, refreshed snapshot replacement, read-error recovery, refusal to refresh after a mapping write attempt, nonparallel/global map registries, rejection of the unsupported multiple-mapping mode, and a GUI-starvation timeout. Both host Python and
Flatpak Python 3.12.13 pass. UI JavaScript syntax was checked with GJS. Dark/light HTML previews and in-page assertions check mismatch/empty/duplicate/valid selection states. An eight-filament layout check confirms that the list scrolls while the footer remains visible. Both requested visual reviews (Sonnet 5.5 via GitHub Copilot and DeepSeek V4.1 Flash Free, high effort) were completed, and their shared feedback on dropdown positioning, state styling, swatches and hierarchy was applied. Real Orca callback dispatch, native sizing, printer permissions and real AFC behavior remain for functional testing. Read-only CC1 fetch and parsing of the supplied two-filament G-code have also passed. No Orca C++ build is needed for this plugin.

The 0.3.1 visual regression checks cover left/center/right-aligned stat groups sharing the preview edges, no redundant assigned-count text, per-option warning colors without inheritance from the selected field, and remaining grams instead of unit names. The dark page surface follows Orca StateColor's #2D2D31 panel mapping; other palette/font/button tokens still come from the live host. The extracted wheel was also imported and rendered with Flatpak Python. Closing still aborts through the host error result: Orca 2.5 exposes no quiet cancellation result at this hook.

In 0.3.6, verification reads actual per-lane maps in one combined object query. This fixes false verification failures after successful SET_MAP swaps and incorrect initial lane metadata caused by treating global AFC.maps as parallel to AFC.lanes. A live no-op Apply/readback of CC1's existing T0 → CANVAS_2 assignment passed with a guard rejecting every write request. No additional printer mutations were performed.

## Permission retry

Orca queues its native permission prompt and rejects a worker request immediately.
It exposes no approval/denial callback to this plugin. Audited GETs pause in the
same dialog: approve access in Orca's prompt, then click the existing Refresh
icon once to retry that specific read. Focus, pointer movement, keyboard activity
and timers do not initiate requests. These proved unreliable as approval signals.

Each paused read gets a token. Refresh consumes that token and disables itself
until a response or a new permission request arrives. Old tokens and unscoped
Refresh events cannot resume another read. If verification is paused after a
mapping write, Refresh can retry only that pending GET; it cannot resubmit the
write or replace the snapshot. Mapping POSTs and unrelated connection errors
are never automatically retried.

The wait is limited to 60 seconds and observes closure, Skip and the UI heartbeat.
A repeated denial ends that attempt; the user can explicitly Refresh again.
Tests cover bounded read retries, wrapped denials, stale/unscoped events, no POST
retries, and Skip during the wait. Rendered UI assertions confirm no retries from
focus/input/timers, single-flight token-bound clicks, and the verification-read
case. Material mismatches remain advisory warnings with Apply enabled.

A mapping POST rejected by Orca's initial `urllib.Request` permission audit is
known not to have reached the network: Flatpak Python raises this event before
`OpenerDirector._open`. The plugin restores its prior write state and offers
Refresh/Skip again. Approve the pending permission, Refresh the current mapping,
then explicitly Apply again. No POST is automatically replayed. Requests do not
follow redirects, so an audit denial cannot refer to a redirected request after
the original POST may already have run. A timeout, later send denial or other
transport failure still retains the uncertain-write state. Tests cover direct and
wrapped initial denials, manual recovery through the same dialog, retention of
uncertainty for later/prior writes, and a local-only redirect server confirming
that the POST is not followed or resubmitted. No live mapping writes were used.
