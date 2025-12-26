#!/usr/bin/env python3
"""
Rules Engine Node

Evaluates rules against track data and emits events/actions.
Rules are defined in YAML configuration files.
"""
import os
import json
import time
from dataclasses import dataclass
from typing import Dict, List, Any, Optional
from pathlib import Path

import yaml
import pyarrow as pa
from dora import Node


@dataclass
class Rule:
    """A single rule definition."""
    name: str
    trigger: Dict[str, Any]
    action: Dict[str, Any]
    cooldown_sec: float = 5.0
    last_triggered: float = 0.0


def point_in_polygon(x: float, y: float, polygon: List[List[float]]) -> bool:
    """Check if point (x,y) is inside polygon using ray casting."""
    n = len(polygon)
    inside = False

    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]

        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i

    return inside


def bbox_center(x: float, y: float, w: float, h: float) -> tuple:
    """Get center point of bounding box."""
    return (x + w / 2, y + h / 2)


class RulesEngineNode:
    """
    Dora node that evaluates rules against track data.

    Inputs:
        - tracks: Enriched track data from tracker_state

    Outputs:
        - events: High-level events (JSON)
        - actions: Commands for downstream systems
        - frame_request: Request frame capture from gst_bridge
    """


    def __init__(self):
        self.node = Node()
        self.rules: List[Rule] = []

        # Track state for duration-based rules
        self.track_zone_entry: Dict[int, Dict[str, float]] = {}  # track_id -> {zone_name: entry_time}

        # Load rules
        rules_path = os.environ.get('RULES_PATH', 'rules/default.yaml')
        self._load_rules(rules_path)

    def _load_rules(self, path: str):
        """Load rules from YAML file."""
        full_path = Path(__file__).parent.parent / path

        if not full_path.exists():
            print(f"[RulesEngine] Rules file not found: {full_path}")
            return

        with open(full_path) as f:
            config = yaml.safe_load(f)

        if not config or 'rules' not in config:
            print("[RulesEngine] No rules defined in config")
            return

        for rule_def in config['rules']:
            rule = Rule(
                name=rule_def['name'],
                trigger=rule_def.get('trigger', {}),
                action=rule_def.get('action', {}),
                cooldown_sec=rule_def.get('cooldown_sec', 5.0)
            )
            self.rules.append(rule)
            print(f"[RulesEngine] Loaded rule: {rule.name}")

        print(f"[RulesEngine] Loaded {len(self.rules)} rules")

    def _evaluate_trigger(self, rule: Rule, track: Dict[str, Any], current_time: float) -> Optional[Dict]:
        """Evaluate if a rule trigger matches a track. Returns event data if triggered."""
        trigger = rule.trigger
        track_id = track['track_id']

        # Class filter
        if 'class' in trigger:
            if track['class_name'] != trigger['class']:
                return None

        # Zone check
        if 'zone' in trigger:
            zone = trigger['zone']
            zone_name = trigger.get('zone_name', rule.name)
            cx, cy = bbox_center(track['x'], track['y'], track['w'], track['h'])

            in_zone = point_in_polygon(cx, cy, zone)

            # Track zone entry/exit
            if track_id not in self.track_zone_entry:
                self.track_zone_entry[track_id] = {}

            zone_times = self.track_zone_entry[track_id]

            if in_zone:
                if zone_name not in zone_times:
                    # Just entered zone
                    zone_times[zone_name] = current_time

                # Check min_duration
                min_duration = trigger.get('min_duration_sec', 0)
                time_in_zone = current_time - zone_times[zone_name]

                if time_in_zone < min_duration:
                    return None

                return {
                    'event_type': 'zone_presence',
                    'zone_name': zone_name,
                    'duration_sec': time_in_zone
                }
            else:
                # Left zone
                if zone_name in zone_times:
                    del zone_times[zone_name]
                return None

        # Velocity check
        if 'min_velocity' in trigger or 'max_velocity' in trigger:
            vx, vy = track['velocity_x'], track['velocity_y']
            speed = (vx**2 + vy**2) ** 0.5

            min_v = trigger.get('min_velocity', 0)
            max_v = trigger.get('max_velocity', float('inf'))

            if not (min_v <= speed <= max_v):
                return None

            return {
                'event_type': 'velocity',
                'speed': speed,
                'velocity': (vx, vy)
            }

        # Stationary check (low velocity for duration)
        if 'stationary_duration_sec' in trigger:
            vx, vy = track['velocity_x'], track['velocity_y']
            speed = (vx**2 + vy**2) ** 0.5
            threshold = trigger.get('velocity_threshold', 0.01)

            if speed > threshold:
                return None

            # Check if stationary long enough (use age_ms as proxy)
            if track['age_ms'] / 1000 < trigger['stationary_duration_sec']:
                return None

            return {
                'event_type': 'stationary',
                'duration_sec': track['age_ms'] / 1000,
                'position': (track['x'], track['y'])
            }

        # Age check
        if 'min_age_sec' in trigger:
            if track['age_ms'] / 1000 < trigger['min_age_sec']:
                return None

            return {
                'event_type': 'age',
                'age_sec': track['age_ms'] / 1000
            }

        # If we got here with no specific trigger, match based on class alone
        if 'class' in trigger:
            return {
                'event_type': 'detection',
                'class': track['class_name']
            }

        return None

    def _execute_action(self, rule: Rule, track: Dict[str, Any], trigger_data: Dict):
        """Execute the action for a triggered rule."""
        action = rule.action
        action_type = action.get('type', 'log')

        current_time = time.time()

        # Build event
        message = action.get('message', f"Rule '{rule.name}' triggered")
        message = message.format(
            track_id=track['track_id'],
            class_name=track['class_name'],
            **trigger_data
        )

        event = {
            'timestamp_ms': current_time * 1000,
            'rule_name': rule.name,
            'track_id': track['track_id'],
            'class_name': track['class_name'],
            'event_type': trigger_data.get('event_type', 'unknown'),
            'message': message,
            'data': json.dumps(trigger_data)
        }

        # Send event as zero-copy Arrow StructArray
        self.node.send_output("events", pa.array([event]))

        if action_type == 'log':
            print(f"[RulesEngine] {message}")

        elif action_type == 'capture_frame':
            # Request frame capture
            self.node.send_output("frame_request", b"1")
            print(f"[RulesEngine] Frame capture requested for: {message}")

        elif action_type == 'webhook':
            # Could implement HTTP webhook here
            url = action.get('url', '')
            print(f"[RulesEngine] Would POST to {url}: {message}")

        # Update last triggered time
        rule.last_triggered = current_time

    def _evaluate_rules(self, tracks: list):
        """Evaluate all rules against all tracks (list of dicts from Arrow)."""
        current_time = time.time()

        for track in tracks:
            for rule in self.rules:
                # Check cooldown
                if current_time - rule.last_triggered < rule.cooldown_sec:
                    continue

                trigger_data = self._evaluate_trigger(rule, track, current_time)
                if trigger_data:
                    self._execute_action(rule, track, trigger_data)

    def run(self):
        """Main loop - process track events and evaluate rules."""
        print("[RulesEngine] Starting...")

        for event in self.node:
            event_type = event["type"]

            if event_type == "INPUT":
                input_id = event["id"]

                if input_id == "tracks":
                    # Zero-copy Arrow - direct access to StructArray as list of dicts
                    tracks = event["value"].to_pylist()

                    # Evaluate rules
                    self._evaluate_rules(tracks)

            elif event_type == "STOP":
                print("[RulesEngine] Stopping")
                break

        print("[RulesEngine] Shutdown complete")


def main():
    node = RulesEngineNode()
    node.run()


if __name__ == '__main__':
    main()
