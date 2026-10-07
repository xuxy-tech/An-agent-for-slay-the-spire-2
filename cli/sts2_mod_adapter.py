from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional


class ModApiError(RuntimeError):
    """The in-game observer/controller Mod rejected or truncated a request."""


@dataclass(frozen=True)
class ModClientConfig:
    base_url: str = "http://127.0.0.1:8080"
    timeout_s: float = 20.0
    required_protocol: str = "2026-03-11-v1"
    rng_base_url: str = "http://127.0.0.1:9877"
    required_rng_protocol: str = "2026-09-19-rng-v1"
    capture_base_url: str = "http://127.0.0.1:9878"
    required_capture_protocol: str = "2026-09-20-human-capture-v4"
    required_capture_patch_count: int = 22


class Sts2ModAdapter:
    """Strict client for the HTTP API exposed inside the visible game.

    Every successful call must use the Mod's standard envelope and contain a
    non-null ``data`` value.  A partial response is an error: callers must never
    turn missing client state into a parity PASS.
    """

    def __init__(self, config: Optional[ModClientConfig] = None):
        self.config = config or ModClientConfig()
        self.last_call_ms: float = 0.0
        self.last_request_id: Optional[str] = None
        self._crystal_sphere_active = False
        self._crystal_sphere_supported: Optional[bool] = None

    def health(self) -> Dict[str, Any]:
        data = self._request("GET", "/health")
        protocol = str(data.get("protocol_version") or "")
        if protocol != self.config.required_protocol:
            raise ModApiError(
                f"Unsupported Mod protocol {protocol!r}; "
                f"expected {self.config.required_protocol!r}"
            )
        if data.get("status") != "ready":
            raise ModApiError(f"Mod is not ready: {data!r}")
        return data

    def rng_health(self) -> Dict[str, Any]:
        data = self._request("GET", "/health", base_url=self.config.rng_base_url)
        protocol = str(data.get("protocol_version") or "")
        if protocol != self.config.required_rng_protocol:
            raise ModApiError(
                f"Unsupported RNG bridge protocol {protocol!r}; "
                f"expected {self.config.required_rng_protocol!r}"
            )
        if data.get("status") != "ready":
            raise ModApiError(f"RNG bridge is not ready: {data!r}")
        return data

    def rng_snapshot(self, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        kwargs = {} if timeout_s is None else {'timeout_s': timeout_s}
        data = self._request("GET", "/rng", base_url=self.config.rng_base_url, **kwargs)
        if data.get("schema_version") != 1 or not isinstance(data.get("run_streams"), dict):
            raise ModApiError(f"Incomplete RNG snapshot: {data!r}")
        if data.get("complete") is not True:
            raise ModApiError(f"RNG bridge returned an incomplete snapshot: {data!r}")
        return data

    def rng_identity(self) -> Dict[str, Any]:
        return self._request("GET", "/identity", base_url=self.config.rng_base_url)

    def capture_health(self) -> Dict[str, Any]:
        data = self._request("GET", "/health", base_url=self.config.capture_base_url)
        protocol = str(data.get("protocol_version") or "")
        if protocol != self.config.required_capture_protocol:
            raise ModApiError(
                f"Unsupported human-capture protocol {protocol!r}; "
                f"expected {self.config.required_capture_protocol!r}"
            )
        if data.get("status") != "ready":
            raise ModApiError(f"Human-capture Mod is not ready: {data!r}")
        patch_count = int(data.get("patch_count") or 0)
        if patch_count < self.config.required_capture_patch_count:
            raise ModApiError(
                f"Human-capture Mod bound only {patch_count} Harmony patches; "
                f"expected at least {self.config.required_capture_patch_count}"
            )
        if data.get("authoritative_combat_snapshot") is not True:
            raise ModApiError("Human-capture Mod does not provide authoritative combat snapshots")
        if data.get("authoritative_run_save") is not True:
            raise ModApiError("Human-capture Mod does not provide authoritative exact saves")
        self._crystal_sphere_supported = (
            data.get('crystal_sphere_contract') == 'sts2.crystal_sphere.v1')
        return data

    def capture_identity(self) -> Dict[str, Any]:
        return self._request("GET", "/identity", base_url=self.config.capture_base_url)

    def action_lifecycle(self, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """Read native GameAction progress on the game thread.

        This endpoint is supplied by the capture Mod, not inferred from the
        observer UI. A missing capability is an error for actions that require
        strong native settlement.
        """
        if timeout_s is None:
            data = self._request("GET", "/action-lifecycle",
                                 base_url=self.config.capture_base_url)
        else:
            data = self._request("GET", "/action-lifecycle",
                                 base_url=self.config.capture_base_url,
                                 timeout_s=timeout_s)
        if (data.get('schema') != 'sts2.native_action_lifecycle.v1'
                or type(data.get('epoch')) is not int
                or type(data.get('revision')) is not int
                or type(data.get('next_action_id')) is not int
                or not isinstance(data.get('actions'), list)
                or type(data.get('queue_empty')) is not bool):
            raise ModApiError(f'Incomplete native action lifecycle: {data!r}')
        return data

    def recover_to_menu(self) -> Dict[str, Any]:
        return self._request("POST", "/recovery/main-menu", {},
                             base_url=self.config.capture_base_url)

    def capture_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
            raise ModApiError("Invalid human-capture snapshot id")
        data = self._request(
            "GET", f"/snapshots/{snapshot_id}", base_url=self.config.capture_base_url
        )
        if data.get("snapshot_id") != snapshot_id or not isinstance(data.get("snapshot_json"), str):
            raise ModApiError(f"Incomplete authoritative combat snapshot: {data!r}")
        return data

    def current_combat_snapshot(self) -> Dict[str, Any]:
        data = self.current_combat_snapshot_raw()
        from controller.human_capture import validate_authoritative_snapshot
        try:
            validate_authoritative_snapshot(data)
        except ValueError as exc:
            # Keep the strict gate, but preserve enough protocol evidence to
            # identify a Mod/schema mismatch without another live request.
            raise ModApiError(
                "Invalid authoritative combat snapshot: "
                f"{exc}; schema={data.get('schema')!r}; "
                f"protocol={data.get('capture_protocol')!r}; "
                f"keys={sorted(data.keys())!r}"
            ) from exc
        return data

    def current_combat_snapshot_raw(self) -> Dict[str, Any]:
        """Return the capture response without weakening snapshot validation."""
        return self._request("GET", "/combat-snapshot/current", base_url=self.config.capture_base_url)

    def exact_save(self) -> Dict[str, Any]:
        """Return a save serialized by the live game at the current boundary.

        ``current_run.save`` is an asynchronous persistence artifact and is
        not a valid synchronization source. The capture Mod exposes the
        game's own ``RunManager.ToSave`` result through this endpoint.
        """
        data = self._request("GET", "/exact-save", base_url=self.config.capture_base_url)
        if data.get("schema") != "sts2.run_save.authoritative.v1":
            raise ModApiError(f"Unsupported authoritative save schema: {data!r}")
        save_json = data.get("save_json")
        if not isinstance(save_json, str) or not save_json:
            raise ModApiError(f"Authoritative save payload is incomplete: {data!r}")
        digest = hashlib.sha256(save_json.encode("utf-8")).hexdigest()
        if data.get("sha256") != digest or data.get("bytes") != len(save_json.encode("utf-8")):
            raise ModApiError("Authoritative save digest or byte count does not match payload")
        return data

    def capture_events(self) -> Iterator[Dict[str, Any]]:
        """Yield exact player-action events from the read-only capture Mod."""
        request = urllib.request.Request(
            self.config.capture_base_url.rstrip("/") + "/events/stream",
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=None) as response:
                event_type = None
                data_lines: list[str] = []
                for raw_line in response:
                    line = raw_line.decode("utf-8").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            try:
                                payload = json.loads("\n".join(data_lines))
                            except json.JSONDecodeError as exc:
                                raise ModApiError(f"Malformed capture event: {exc}") from exc
                            if not isinstance(payload, dict):
                                raise ModApiError("Capture event payload is not an object")
                            if event_type and payload.get("type") != event_type:
                                raise ModApiError("Capture event name does not match its envelope")
                            yield payload
                        event_type, data_lines = None, []
                        continue
                    if line.startswith(":"):
                        continue
                    field, separator, value = line.partition(":")
                    if not separator:
                        continue
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        event_type = value
                    elif field == "data":
                        data_lines.append(value)
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
            raise ModApiError(f"Human-capture event stream failed: {exc}") from exc

    def state(self, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        kwargs = {} if timeout_s is None else {'timeout_s': timeout_s}
        data = self._request("GET", "/state", **kwargs)
        required = ("state_version", "run_id", "screen", "available_actions")
        missing = [key for key in required if key not in data]
        if missing:
            raise ModApiError(f"Incomplete /state response; missing {missing}")
        if not isinstance(data.get("available_actions"), list):
            raise ModApiError("Incomplete /state response; available_actions is not a list")
        data = self._enrich_rewards(data, deadline=deadline)
        self._crystal_sphere_active = False
        if data.get('screen') == 'UNKNOWN' and self._crystal_sphere_supported is not False:
            try:
                native = self._request(
                    'GET', '/crystal-sphere/state', base_url=self.config.capture_base_url,
                    **kwargs)
            except ModApiError as exc:
                # Older capture Mods do not have this optional endpoint.
                # A failure from a Mod that does expose it must stay visible.
                if "404" in str(exc):
                    self._crystal_sphere_supported = False
                    return data
                raise
            if native.get('contract') == 'sts2.crystal_sphere.v1' and native.get('active') is True:
                self._crystal_sphere_supported = True
                phase = native.get('phase')
                data['screen'] = 'CRYSTAL_SPHERE'
                data['crystal_sphere'] = native
                data['available_actions'] = (
                    ['crystal_sphere_divine'] if phase == 'divining'
                    else ['proceed'] if phase == 'proceed' else []
                ) + [action for action in data['available_actions']
                     if action in {'discard_potion', 'use_potion'}]
                self._crystal_sphere_active = True
        return data

    def _enrich_rewards(self, state: Dict[str, Any], *, deadline: Optional[float] = None) -> Dict[str, Any]:
        reward = state.get('reward')
        if not isinstance(reward, dict):
            return state
        kwargs = {}
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModApiError('State deadline expired before native reward observation')
            kwargs['timeout_s'] = remaining
        native = self._request('GET', '/reward-state', base_url=self.config.capture_base_url, **kwargs)
        if (native.get('contract') != 'native-reward-items-v1'
                or type(native.get('reward_set_id')) is not int
                or native.get('pending_card_choice') != reward.get('pending_card_choice')):
            raise ModApiError('Reward screen changed or native reward identity is unavailable')
        visible = reward.get('rewards') or []
        native_rows = native.get('rewards') or []
        if [(r.get('index'), r.get('reward_type')) for r in visible] != [
                (r.get('index'), r.get('reward_type')) for r in native_rows]:
            raise ModApiError('Complete ordered reward buttons differ from native reward observation')
        for row, identity in zip(visible, native_rows):
            row.update(identity)
        reward.update(reward_set_id=native['reward_set_id'],
                      reward_index=native.get('reward_index'),
                      offered_rewards=native.get('offered_rewards'))
        return state

    def available_actions(self) -> Dict[str, Any]:
        data = self._request("GET", "/actions/available")
        if "screen" not in data or not isinstance(data.get("actions"), list):
            raise ModApiError("Incomplete /actions/available response")
        return data

    def action(self, action: str, *, allow_pending_selection: bool = False,
               timeout_s: Optional[float] = None, **params: Any) -> Dict[str, Any]:
        return self._action(action, allow_pending_selection=allow_pending_selection,
                            timeout_s=timeout_s, **params)

    def submit_action(self, action: str, *, timeout_s: Optional[float] = None,
                      **params: Any) -> Dict[str, Any]:
        """Return an acknowledgement; the flow executor owns completion polling."""
        if action == 'crystal_sphere_divine' or (action == 'proceed' and self._crystal_sphere_active):
            return self._crystal_sphere_action(action, params, timeout_s=timeout_s)
        return self._action(action, accept_pending=True, timeout_s=timeout_s, **params)

    def _crystal_sphere_action(self, action: str, params: Dict[str, Any],
                               *, timeout_s: Optional[float]) -> Dict[str, Any]:
        native = self._request('POST', '/crystal-sphere/action',
                               {'action': action, **params},
                               base_url=self.config.capture_base_url, timeout_s=timeout_s)
        if native.get('action') != action or native.get('status') != 'submitted':
            raise ModApiError(f'Incomplete Crystal Sphere action response: {native!r}')
        state = self.state(timeout_s=timeout_s)
        return {'action': action, 'status': 'completed', 'stable': True, 'state': state}

    def _action(self, action: str, *, allow_pending_selection: bool = False,
                accept_pending: bool = False, timeout_s: Optional[float] = None,
                **params: Any) -> Dict[str, Any]:
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        body: Dict[str, Any] = {"action": action}
        body.update({key: value for key, value in params.items() if value is not None})
        if timeout_s is None:
            # Keep the legacy adapter/test-double call shape when no explicit
            # transaction budget is active.
            data = self._request("POST", "/action", body)
        else:
            data = self._request("POST", "/action", body, timeout_s=timeout_s)
        required = ("action", "status", "stable", "state")
        missing = [key for key in required if key not in data]
        if missing:
            raise ModApiError(f"Incomplete /action response; missing {missing}")
        if data.get("action") != action:
            raise ModApiError(
                f"Action response mismatch: requested {action!r}, got {data.get('action')!r}"
            )
        selection_pending = (allow_pending_selection and action == 'select_deck_card'
                             and data.get('stable') is False and data.get('status') == 'pending')
        state = data.get('state') or {}
        combat_selection = (action in {'play_card', 'use_potion'} and data.get('status') == 'pending'
                            and data.get('stable') is False and state.get('in_combat') is True
                            and state.get('screen') == 'CARD_SELECTION'
                            and 'select_deck_card' in (state.get('available_actions') or [])
                            and isinstance(state.get('selection'), dict) and bool(state['selection'].get('cards')))
        selection_pending = selection_pending or combat_selection or (
            accept_pending and data.get('status') == 'pending' and data.get('stable') is False)
        if not selection_pending and (data.get("stable") is not True or data.get("status") != "completed"):
            raise ModApiError(
                f"Client action did not reach a stable completed state: "
                f"status={data.get('status')!r}, stable={data.get('stable')!r}"
            )
        if not isinstance(data.get("state"), dict):
            raise ModApiError("Incomplete /action response; state is not an object")
        data['state'] = self._enrich_rewards(data['state'], deadline=deadline)
        return data

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        base_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        raw_body = None
        headers = {"Accept": "application/json"}
        if body is not None:
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            (base_url or self.config.base_url).rstrip("/") + path,
            data=raw_body,
            headers=headers,
            method=method,
        )
        started = time.perf_counter()
        try:
            request_timeout = self.config.timeout_s if timeout_s is None else max(0.01, float(timeout_s))
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                details = json.loads(exc.read().decode("utf-8"))
            except Exception:
                details = {"status": exc.code, "reason": str(exc.reason)}
            raise ModApiError(f"Mod API {method} {path} failed: {details}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ModApiError(f"Mod API {method} {path} failed: {exc}") from exc
        finally:
            self.last_call_ms = (time.perf_counter() - started) * 1000.0

        if not isinstance(payload, dict):
            raise ModApiError(f"Mod API {method} {path} returned a non-object envelope")
        self.last_request_id = payload.get("request_id")
        if payload.get("ok") is not True:
            raise ModApiError(f"Mod API {method} {path} returned an error: {payload}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ModApiError(f"Mod API {method} {path} returned no complete data object")
        return data
