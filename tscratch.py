"""Tiled Scratchpads plugin for Pyprland.

Provides compound floating scratchpads with dynamic proportional tiling,
multi-window layouts (columns, master-stack), orientation (horizontal/vertical),
smart focus restoration, auto-hide on focus loss, smooth animations,
and runtime CLI controls.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from pyprland.adapters.units import convert_monitor_dimension
from pyprland.models import ClientInfo, Environment, MonitorInfo
from pyprland.plugins.interface import Plugin


class WindowItem:
    def __init__(self, name: str, group: str, config: dict[str, Any]):
        self.name = name
        self.group = group
        self.config = config
        self.command: str = config.get("command", "")
        self.match_by: str = config.get("match_by", "class")
        self.match_value: str = config.get(self.match_by, "")
        self.ratio: float = float(config.get("ratio", 1.0))
        self.animation: str = config.get("animation", "").lower()
        self.close_on_hide: bool = config.get("close_on_hide", False)

        # Min and Max constraints
        self.min_width: str = config.get("min_width", "")
        self.max_width: str = config.get("max_width", "")
        self.min_height: str = config.get("min_height", "")
        self.max_height: str = config.get("max_height", "")

        # Solo placement options
        self.solo_position: str = config.get("solo_position", "center" if config.get("solo_center") else config.get("sidebar", "center")).lower()
        self.solo_width: str = config.get("solo_width", "")
        self.solo_height: str = config.get("solo_height", "")

        self.address: str = ""
        self.visible: bool = False
        self.pinned: bool = config.get("pinned", False)

    def get_min_w(self, mon_w: int, monitor: MonitorInfo) -> int | None:
        if self.min_width:
            return convert_monitor_dimension(self.min_width, mon_w, monitor, logical_ref=True)
        return None

    def get_max_w(self, mon_w: int, monitor: MonitorInfo) -> int | None:
        if self.max_width:
            return convert_monitor_dimension(self.max_width, mon_w, monitor, logical_ref=True)
        return None

    def get_min_h(self, mon_h: int, monitor: MonitorInfo) -> int | None:
        if self.min_height:
            return convert_monitor_dimension(self.min_height, mon_h, monitor, logical_ref=True)
        return None

    def get_max_h(self, mon_h: int, monitor: MonitorInfo) -> int | None:
        if self.max_height:
            return convert_monitor_dimension(self.max_height, mon_h, monitor, logical_ref=True)
        return None

    def clamp(self, w: int, h: int, mon_w: int, mon_h: int, monitor: MonitorInfo) -> tuple[int, int]:
        min_w = self.get_min_w(mon_w, monitor)
        max_w = self.get_max_w(mon_w, monitor)
        min_h = self.get_min_h(mon_h, monitor)
        max_h = self.get_max_h(mon_h, monitor)
        if min_w is not None:
            w = max(w, min_w)
        if max_w is not None:
            w = min(w, max_w)
        if min_h is not None:
            h = max(h, min_h)
        if max_h is not None:
            h = min(h, max_h)
        return w, h

    def matches(self, client: ClientInfo) -> bool:
        if not self.match_value:
            val = self.config.get("class") or self.config.get("initialClass")
            if not val:
                return False
            return client.get("class") == val or client.get("initialClass") == val
        return client.get(self.match_by) == self.match_value


class TiledGroup:
    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.width_str: str = config.get("width", "94%")
        self.height_str: str = config.get("height", "88%")
        self.top_str: str = config.get("top", "6%")
        self.left_str: str = config.get("left", "")
        self.gap: int = int(config.get("gap", 14))

        # Layout & Orientation
        self.orientation: str = config.get("orientation", "horizontal").lower()  # "horizontal" | "vertical"
        self.layout: str = config.get("layout", "columns").lower()  # "columns" | "master-stack"
        self.master_ratio: float = float(config.get("master_ratio", 0.7))
        self.hide_on_focus_lost: bool = config.get("hide_on_focus_lost", False)
        self.restore_focus: bool = config.get("restore_focus", True)
        self.pinned: bool = config.get("pinned", False)
        self.monitor: str = config.get("monitor", "active")

        # Ratio presets for runtime cycling
        self.presets: list[list[float]] = config.get("presets", [])
        self._preset_index: int = 0

        # Windows
        self.windows: dict[str, WindowItem] = {}
        windows_conf = config.get("windows", {})

        # Configured order if provided
        configured_order = config.get("order", list(windows_conf.keys()))
        for w_name in configured_order:
            if w_name in windows_conf:
                self.windows[w_name] = WindowItem(w_name, name, windows_conf[w_name])
        # Add any windows not in explicit order
        for w_name, w_conf in windows_conf.items():
            if w_name not in self.windows:
                self.windows[w_name] = WindowItem(w_name, name, w_conf)


class Extension(Plugin, environments=[Environment.HYPRLAND]):
    """Tiled Scratchpads plugin for Pyprland."""

    groups: dict[str, TiledGroup]
    _lock: asyncio.Lock
    previously_focused_window: str
    _animating: set[str]

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.groups = {}
        self._lock = asyncio.Lock()
        self.previously_focused_window = ""
        self._animating = set()

    async def init(self) -> None:
        self.log.info("Initializing Tiled Scratchpads plugin")

    async def load_config(self, config: dict[str, Any]) -> None:
        self.config.clear()
        raw = config.get("tscratch") or config.get("tiled_scratchpads") or config.get(self.name) or {}
        self.config.update(raw)
        self.log.info("tscratch loaded config sections: %s", list(self.config.keys()))

    async def on_reload(self, reason: Any = None) -> None:
        self.groups.clear()
        for group_name, group_conf in self.config.items():
            if isinstance(group_conf, dict) and "windows" in group_conf:
                self.groups[group_name] = TiledGroup(group_name, group_conf)
                new_group = self.groups[group_name]
                for preset in new_group.presets:
                    if len(preset) != len(new_group.windows):
                        self.log.warning(
                            "Group '%s' has a preset with %d values but %d windows; this preset will be ignored",
                            group_name,
                            len(preset),
                            len(new_group.windows),
                        )
                self.log.info(
                    "Configured group '%s' (orientation: %s, layout: %s, windows: %s)",
                    group_name,
                    self.groups[group_name].orientation,
                    self.groups[group_name].layout,
                    list(self.groups[group_name].windows.keys()),
                )

    def _find_window(self, window_name: str) -> tuple[TiledGroup, WindowItem] | tuple[None, None]:
        for group in self.groups.values():
            if window_name in group.windows:
                return group, group.windows[window_name]
        return None, None

    def _find_group_by_address(self, addr: str) -> tuple[TiledGroup, WindowItem] | tuple[None, None]:
        clean_addr = "0x" + addr.removeprefix("0x")
        for group in self.groups.values():
            for item in group.windows.values():
                if item.address and (item.address == clean_addr or item.address.removeprefix("0x") == addr.removeprefix("0x")):
                    return group, item
        return None, None

    async def _get_matching_client(self, item: WindowItem, clients: list[ClientInfo] | None = None) -> ClientInfo | None:
        if clients is None:
            clients = await self.backend.get_clients()
        if item.address:
            for c in clients:
                if c["address"] == item.address and item.matches(c):
                    return c
        # Prefer matching clients parked in this plugin's special workspaces to
        # avoid latching onto unrelated user windows sharing the same class.
        def parked(c: ClientInfo) -> bool:
            ws = c.get("workspace", {})
            return isinstance(ws, dict) and (ws.get("name") or "").startswith("special:tscratch")

        for c in clients:
            if item.matches(c) and parked(c):
                item.address = c["address"]
                return c
        for c in clients:
            if item.matches(c):
                item.address = c["address"]
                return c
        return None

    async def _ensure_window_alive(self, item: WindowItem) -> ClientInfo:
        client = await self._get_matching_client(item)
        if client:
            return client

        self.log.info("Spawning window for '%s': %s", item.name, item.command)
        await self.backend.execute(f"exec {item.command}")

        for _ in range(50):
            await asyncio.sleep(0.1)
            client = await self._get_matching_client(item)
            if client:
                self.log.info("Window '%s' ready at %s", item.name, client["address"])
                if not client.get("floating", False):
                    await self.backend.execute(f"setfloating address:{client['address']}")
                return client

        raise RuntimeError(f"Timed out waiting for scratchpad '{item.name}'")

    async def _get_target_monitor(self, group: TiledGroup) -> MonitorInfo:
        mon_name = group.monitor if group.monitor != "active" else self.state.active_monitor
        mon = await self.backend.get_monitor_props(name=mon_name)
        if not mon:
            monitors = await self.backend.get_monitors()
            mon = monitors[0] if monitors else {"width": 1920, "height": 1080, "scale": 1.0, "x": 0, "y": 0, "name": "default"}
        return mon

    def _solve_dimension(
        self,
        items: list[WindowItem],
        total_space: int,
        ref_dim: int,
        monitor: MonitorInfo,
        axis: str = "width",
    ) -> dict[str, int]:
        """Proportional allocation respecting min and max constraints."""
        unconstrained = list(items)
        sizes: dict[str, int] = {}
        remaining_space = total_space

        while unconstrained:
            total_ratio = sum(it.ratio for it in unconstrained) or 1.0
            clamped_any = False
            for it in list(unconstrained):
                ideal = int(remaining_space * (it.ratio / total_ratio))
                min_val = it.get_min_w(ref_dim, monitor) if axis == "width" else it.get_min_h(ref_dim, monitor)
                max_val = it.get_max_w(ref_dim, monitor) if axis == "width" else it.get_max_h(ref_dim, monitor)

                if min_val is not None and ideal < min_val:
                    sizes[it.name] = min_val
                    remaining_space -= min_val
                    unconstrained.remove(it)
                    clamped_any = True
                    break
                elif max_val is not None and ideal > max_val:
                    sizes[it.name] = max_val
                    remaining_space -= max_val
                    unconstrained.remove(it)
                    clamped_any = True
                    break

            if not clamped_any:
                for i, it in enumerate(unconstrained):
                    if i == len(unconstrained) - 1:
                        sizes[it.name] = max(0, remaining_space)
                    else:
                        sz = max(0, int(remaining_space * (it.ratio / total_ratio)))
                        sizes[it.name] = sz
                        remaining_space -= sz
                break

        return sizes

    async def _calculate_geometry(
        self,
        group: TiledGroup,
        visible_items: list[WindowItem],
        monitor: MonitorInfo,
    ) -> dict[str, tuple[int, int, int, int]]:
        result: dict[str, tuple[int, int, int, int]] = {}
        if not visible_items:
            return result

        mon_w = int(monitor["width"] / monitor["scale"])
        mon_h = int(monitor["height"] / monitor["scale"])
        mon_x = monitor["x"]
        mon_y = monitor["y"]

        cont_w = convert_monitor_dimension(group.width_str, mon_w, monitor, logical_ref=True)
        cont_h = convert_monitor_dimension(group.height_str, mon_h, monitor, logical_ref=True)
        cont_y = mon_y + convert_monitor_dimension(group.top_str, mon_h, monitor, logical_ref=True)
        cont_x = mon_x + (convert_monitor_dimension(group.left_str, mon_w, monitor, logical_ref=True) if group.left_str else (mon_w - cont_w) // 2)

        # Solo Window Layout
        if len(visible_items) == 1:
            item = visible_items[0]
            pos = item.solo_position or "center"

            # Auto fallback to max_width or container width if omitted
            if not item.solo_width or item.solo_width in ("auto", "max"):
                raw_w = item.get_max_w(mon_w, monitor) or cont_w
            else:
                raw_w = convert_monitor_dimension(item.solo_width, mon_w, monitor, logical_ref=True)

            # Auto fallback to max_height or container height if omitted
            if not item.solo_height or item.solo_height in ("auto", "max"):
                raw_h = item.get_max_h(mon_h, monitor) or cont_h
            else:
                raw_h = convert_monitor_dimension(item.solo_height, mon_h, monitor, logical_ref=True)

            w, h = item.clamp(raw_w, raw_h, mon_w, mon_h, monitor)

            if pos == "right":
                x = cont_x + (cont_w - w)
                y = cont_y + (cont_h - h) // 2
            elif pos == "left":
                x = cont_x
                y = cont_y + (cont_h - h) // 2
            elif pos == "top":
                x = mon_x + (mon_w - w) // 2
                y = cont_y
            elif pos == "bottom":
                x = mon_x + (mon_w - w) // 2
                y = cont_y + (cont_h - h)
            else:  # "center"
                x = mon_x + (mon_w - w) // 2
                y = mon_y + (mon_h - h) // 2

            result[item.name] = (x, y, w, h)
            return result

        ordered = [w for w in group.windows.values() if w in visible_items]
        num_windows = len(ordered)

        # Master-Stack Layout (for 3+ windows or explicit master-stack)
        if group.layout == "master-stack" and num_windows > 2:
            master = ordered[0]
            stack = ordered[1:]
            if group.orientation == "horizontal":
                master_w_raw = int((cont_w - group.gap) * group.master_ratio)
                master_w, master_h = master.clamp(master_w_raw, cont_h, mon_w, mon_h, monitor)
                stack_w = max(0, cont_w - master_w - group.gap)
                result[master.name] = (cont_x, cont_y, master_w, master_h)

                total_stack_gaps = group.gap * (len(stack) - 1)
                avail_stack_h = max(0, cont_h - total_stack_gaps)
                stack_sizes = self._solve_dimension(stack, avail_stack_h, mon_h, monitor, axis="height")
                curr_y = cont_y
                for s in stack:
                    h = stack_sizes.get(s.name, 0)
                    w, _ = s.clamp(stack_w, h, mon_w, mon_h, monitor)
                    result[s.name] = (cont_x + master_w + group.gap, curr_y, w, h)
                    curr_y += h + group.gap
            else:  # Vertical orientation master-stack
                master_h_raw = int((cont_h - group.gap) * group.master_ratio)
                master_w, master_h = master.clamp(cont_w, master_h_raw, mon_w, mon_h, monitor)
                stack_h = max(0, cont_h - master_h - group.gap)
                result[master.name] = (cont_x, cont_y, master_w, master_h)

                total_stack_gaps = group.gap * (len(stack) - 1)
                avail_stack_w = max(0, cont_w - total_stack_gaps)
                stack_sizes = self._solve_dimension(stack, avail_stack_w, mon_w, monitor, axis="width")
                curr_x = cont_x
                for s in stack:
                    w = stack_sizes.get(s.name, 0)
                    _, h = s.clamp(w, stack_h, mon_w, mon_h, monitor)
                    result[s.name] = (curr_x, cont_y + master_h + group.gap, w, h)
                    curr_x += w + group.gap
            return result

        # Columns / Linear Tiling Layout (Horizontal or Vertical)
        if group.orientation == "vertical":
            total_gaps = group.gap * (num_windows - 1)
            avail_h = cont_h - total_gaps
            sizes = self._solve_dimension(ordered, avail_h, mon_h, monitor, axis="height")
            curr_y = cont_y
            for item in ordered:
                h = sizes.get(item.name, 0)
                w, _ = item.clamp(cont_w, h, mon_w, mon_h, monitor)
                result[item.name] = (cont_x, curr_y, w, h)
                curr_y += h + group.gap
        else:  # Horizontal (default)
            total_gaps = group.gap * (num_windows - 1)
            avail_w = cont_w - total_gaps
            sizes = self._solve_dimension(ordered, avail_w, mon_w, monitor, axis="width")
            curr_x = cont_x
            for item in ordered:
                w = sizes.get(item.name, 0)
                _, h = item.clamp(w, cont_h, mon_w, mon_h, monitor)
                result[item.name] = (curr_x, cont_y, w, h)
                curr_x += w + group.gap

        return result

    def _get_offscreen_position(self, anim: str, x: int, y: int, w: int, h: int, monitor: MonitorInfo) -> tuple[int, int]:
        mon_w = int(monitor["width"] / monitor["scale"])
        mon_h = int(monitor["height"] / monitor["scale"])
        mon_x = monitor["x"]
        mon_y = monitor["y"]
        if anim == "fromtop":
            return x, mon_y - h - 50
        elif anim == "frombottom":
            return x, mon_y + mon_h + 50
        elif anim == "fromleft":
            return mon_x - w - 50, y
        elif anim == "fromright":
            return mon_x + mon_w + 50, y
        return x, y

    async def _apply_layout(self, group: TiledGroup, animate_in: WindowItem | None = None) -> None:
        visible_items = [w for w in group.windows.values() if w.visible]
        monitor = await self._get_target_monitor(group)
        geometries = await self._calculate_geometry(group, visible_items, monitor)
        active_ws = monitor.get("activeWorkspace", {}).get("name", self.state.active_workspace)

        # Offscreen pre-positioning for smooth slide-in animation
        if animate_in and animate_in.visible and animate_in.animation and animate_in.name in geometries:
            target_x, target_y, target_w, target_h = geometries[animate_in.name]
            off_x, off_y = self._get_offscreen_position(animate_in.animation, target_x, target_y, target_w, target_h, monitor)
            await self.backend.execute([
                f"tagwindow +pypr_noanim address:{animate_in.address}",
                f"movetoworkspacesilent {active_ws},address:{animate_in.address}",
                f"resizewindowpixel exact {target_w} {target_h},address:{animate_in.address}",
                f"movewindowpixel exact {off_x} {off_y},address:{animate_in.address}",
            ])
            await asyncio.sleep(0.016)  # 1 frame delay
            await self.backend.execute(f"tagwindow -pypr_noanim address:{animate_in.address}")

        clients = await self.backend.get_clients()
        client_map = {c["address"]: c for c in clients}

        batch_commands: list[str] = []
        for item in group.windows.values():
            is_pinned = bool(client_map.get(item.address, {}).get("pinned", False)) if item.address else False
            if item.visible and item.name in geometries:
                x, y, w, h = geometries[item.name]
                batch_commands.append(f"movetoworkspacesilent {active_ws},address:{item.address}")
                batch_commands.append(f"alterzorder top,address:{item.address}")
                batch_commands.append(f"resizewindowpixel exact {w} {h},address:{item.address}")
                batch_commands.append(f"movewindowpixel exact {x} {y},address:{item.address}")
                if (item.pinned or group.pinned) and not is_pinned:
                    batch_commands.append(f"pin address:{item.address}")
            elif not item.visible and item.address:
                if item.close_on_hide:
                    batch_commands.append(f"closewindow address:{item.address}")
                    item.address = ""
                else:
                    if is_pinned:
                        batch_commands.append(f"pin address:{item.address}")
                    batch_commands.append(f"movetoworkspacesilent special:tscratch_{item.name},address:{item.address}")

        if batch_commands:
            await self.backend.execute(batch_commands)

    # CLI Handlers
    async def run_tscratch(self, *args: str) -> str:
        """General dispatcher: pypr tscratch <action> [args...]"""
        if not args:
            return "Usage: pypr tscratch <toggle|show|hide|toggle_all|hide_all|ratio|cycle_ratio|cycle|swap> [args]"

        tokens = " ".join(args).split()
        cmd = tokens[0]
        cmd_args = tokens[1:]

        if cmd == "toggle":
            return await self.run_tscratch_toggle(cmd_args[0] if cmd_args else "")
        elif cmd == "show":
            return await self.run_tscratch_show(cmd_args[0] if cmd_args else "")
        elif cmd == "hide":
            return await self.run_tscratch_hide(cmd_args[0] if cmd_args else "")
        elif cmd == "toggle_all":
            return await self.run_tscratch_toggle_all(cmd_args[0] if cmd_args else "")
        elif cmd == "hide_all":
            return await self.run_tscratch_hide_all()
        elif cmd == "ratio":
            return await self.run_tscratch_ratio(*cmd_args)
        elif cmd == "cycle_ratio":
            return await self.run_tscratch_cycle_ratio(cmd_args[0] if cmd_args else "")
        elif cmd == "cycle":
            return await self.run_tscratch_cycle(cmd_args[0] if cmd_args else "")
        elif cmd == "swap":
            return await self.run_tscratch_swap(cmd_args[0] if len(cmd_args) > 0 else "", cmd_args[1] if len(cmd_args) > 1 else "")
        return f"Unknown tscratch command: {cmd}"

    async def run_tscratch_toggle(self, window_name: str = "") -> str:
        if not window_name:
            return await self.run_tscratch_toggle_all()

        async with self._lock:
            group, item = self._find_window(window_name)
            if not group or not item:
                return f"Window '{window_name}' not found in any group"

            client = await self._ensure_window_alive(item)
            item.address = client["address"]

            monitor = await self._get_target_monitor(group)
            active_ws = monitor.get("activeWorkspace", {}).get("name", self.state.active_workspace)

            clients = await self.backend.get_clients()
            curr_client = next((c for c in clients if c["address"] == item.address), client)
            is_on_active = curr_client.get("workspace", {}).get("name") == active_ws

            animate_in = None
            if item.visible and is_on_active:
                # Sliding hide transition if configured
                if item.animation:
                    curr_at = curr_client.get("at", [0, 0])
                    curr_size = curr_client.get("size", [0, 0])
                    off_x, off_y = self._get_offscreen_position(item.animation, curr_at[0], curr_at[1], curr_size[0], curr_size[1], monitor)
                    await self.backend.execute(f"movewindowpixel exact {off_x} {off_y},address:{item.address}")
                    await asyncio.sleep(0.12)
                item.visible = False
            else:
                item.visible = True
                animate_in = item

            await self._apply_layout(group, animate_in=animate_in)

            if item.visible:
                await self.backend.focus_window(item.address)
            elif group.restore_focus and self.previously_focused_window:
                await self.backend.focus_window(self.previously_focused_window)

            return f"Toggled {window_name} -> {'visible' if item.visible else 'hidden'}"

    async def run_tscratch_show(self, window_name: str) -> str:
        async with self._lock:
            group, item = self._find_window(window_name)
            if not group or not item:
                return f"Window '{window_name}' not found"
            client = await self._ensure_window_alive(item)
            item.address = client["address"]
            if item.visible:
                await self.backend.focus_window(item.address)
                return f"Showing {window_name} (already visible)"
            item.visible = True
            await self._apply_layout(group, animate_in=item)
            await self.backend.focus_window(item.address)
            return f"Showing {window_name}"

    async def run_tscratch_hide(self, window_name: str) -> str:
        async with self._lock:
            group, item = self._find_window(window_name)
            if not group or not item:
                return f"Window '{window_name}' not found"
            item.visible = False
            await self._apply_layout(group)
            if group.restore_focus and self.previously_focused_window:
                await self.backend.focus_window(self.previously_focused_window)
            return f"Hidden {window_name}"

    async def run_tscratch_toggle_all(self, group_name: str = "") -> str:
        async with self._lock:
            return await self._toggle_group(group_name)

    async def _toggle_group(self, group_name: str = "") -> str:
        """Toggle a group's windows. Caller must hold self._lock."""
        if group_name:
            group = self.groups.get(group_name)
        else:
            group = next(iter(self.groups.values()), None)
        if not group:
            return "No groups configured" if not group_name else f"Group '{group_name}' not found"

        any_visible = any(w.visible for w in group.windows.values())
        for w in group.windows.values():
            if any_visible:
                w.visible = False
            else:
                await self._ensure_window_alive(w)
                w.visible = True
        await self._apply_layout(group)
        if any_visible and group.restore_focus and self.previously_focused_window:
            await self.backend.focus_window(self.previously_focused_window)
        return f"Toggled group '{group.name}' -> {'hidden' if any_visible else 'visible'}"

    async def run_tscratch_hide_all(self) -> str:
        async with self._lock:
            for group in self.groups.values():
                for w in group.windows.values():
                    w.visible = False
                await self._apply_layout(group)
            if self.previously_focused_window:
                await self.backend.focus_window(self.previously_focused_window)
            return "Hidden all scratchpads"

    async def run_tscratch_ratio(self, *args: str) -> str:
        """Set runtime tiling ratios: pypr tscratch ratio <group> 0.7 0.3"""
        async with self._lock:
            tokens = " ".join(args).split()
            if not tokens:
                return "Usage: pypr tscratch ratio <group> <r1> <r2> ..."
            group_name, ratios = tokens[0], tokens[1:]
            group = self.groups.get(group_name)
            if not group:
                return f"Group '{group_name}' not found"
            if len(ratios) != len(group.windows):
                return f"Expected {len(group.windows)} ratio values for group '{group_name}'"

            try:
                new_ratios = [float(r) for r in ratios]
            except ValueError:
                return f"Invalid ratio values: {', '.join(ratios)} (use numbers like 0.7)"

            for w, r in zip(group.windows.values(), new_ratios):
                w.ratio = r
            await self._apply_layout(group)
            return f"Updated ratios for '{group_name}' to {new_ratios}"

    async def run_tscratch_cycle_ratio(self, group_name: str = "") -> str:
        """Cycle through configured presets for a group."""
        async with self._lock:
            group = self.groups.get(group_name) or next(iter(self.groups.values()), None)
            if not group:
                return f"Group '{group_name}' not found"

            presets = group.presets or [
                [0.8, 0.2],
                [0.5, 0.5],
                [0.2, 0.8],
            ]
            valid = [p for p in presets if len(p) == len(group.windows)]
            if not valid:
                return f"Presets for '{group.name}' must have {len(group.windows)} values each"

            target_preset = valid[group._preset_index % len(valid)]
            group._preset_index = (group._preset_index + 1) % len(valid)

            for w, r in zip(group.windows.values(), target_preset):
                w.ratio = float(r)
            await self._apply_layout(group)
            return f"Cycled '{group.name}' to preset: {target_preset}"

    async def run_tscratch_cycle(self, group_name: str = "") -> str:
        """Focus the next visible window in the group."""
        async with self._lock:
            group = self.groups.get(group_name) or next(iter(self.groups.values()), None)
            if not group:
                return "No groups configured" if not group_name else f"Group '{group_name}' not found"
            visible = [w for w in group.windows.values() if w.visible and w.address]
            if not visible:
                return "No visible windows in group"

            curr_focused = self.state.active_window
            curr_idx = -1
            for i, w in enumerate(visible):
                if w.address == curr_focused:
                    curr_idx = i
                    break
            next_idx = (curr_idx + 1) % len(visible)
            await self.backend.focus_window(visible[next_idx].address)
            return f"Focused {visible[next_idx].name}"

    async def run_tscratch_swap(self, *args: str) -> str:
        """Swap positions of two windows in their group."""
        tokens = " ".join(args).split() if args else []
        if len(tokens) != 2:
            return "Usage: pypr tscratch swap <win1> <win2>"
        win1_name, win2_name = tokens

        async with self._lock:
            g1, w1 = self._find_window(win1_name)
            g2, w2 = self._find_window(win2_name)
            if not g1 or not g2 or g1 != g2:
                return "Both windows must exist and belong to the same group"

            keys = list(g1.windows.keys())
            idx1, idx2 = keys.index(win1_name), keys.index(win2_name)
            keys[idx1], keys[idx2] = keys[idx2], keys[idx1]

            new_windows = {k: g1.windows[k] for k in keys}
            g1.windows = new_windows
            await self._apply_layout(g1)
            return f"Swapped {win1_name} and {win2_name}"

    # Compositor Event Hooks
    async def event_activewindowv2(self, addr: str) -> None:
        """Track focus for restore_focus and auto-hide when clicking outside."""
        clean_addr = "0x" + addr.removeprefix("0x")
        is_scratchpad = any(
            w.address and (w.address == clean_addr or w.address.removeprefix("0x") == addr.removeprefix("0x"))
            for g in self.groups.values()
            for w in g.windows.values()
        )

        if not is_scratchpad:
            self.previously_focused_window = clean_addr
            # Check groups with hide_on_focus_lost
            async with self._lock:
                for group in self.groups.values():
                    if group.hide_on_focus_lost and any(w.visible for w in group.windows.values()):
                        for w in group.windows.values():
                            w.visible = False
                        await self._apply_layout(group)

    async def event_closewindow(self, addr: str) -> None:
        """Handle window closed by user (e.g. SUPER + Q)."""
        group, item = self._find_group_by_address(addr)
        if item and group:
            self.log.info("Scratchpad window '%s' closed", item.name)
            async with self._lock:
                item.address = ""
                item.visible = False
                await self._apply_layout(group)

    async def event_monitoradded(self, monitor_name: str) -> None:
        """Recalculate layout when a monitor is connected."""
        async with self._lock:
            for group in self.groups.values():
                if any(w.visible for w in group.windows.values()):
                    await self._apply_layout(group)

    async def event_monitorremoved(self, monitor_name: str) -> None:
        """Recalculate layout when a monitor is disconnected."""
        async with self._lock:
            for group in self.groups.values():
                if any(w.visible for w in group.windows.values()):
                    await self._apply_layout(group)
