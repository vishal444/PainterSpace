"""Queue compaction preserves the planning inputs while avoiding repeated inventories."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools' / 'shot_library'))

from function_catalog import FunctionCatalog
from llm_planner import LLMShotPlanner
from process_agent_queue import write_task_json


def test_compact_request_round_trips_scene_catalog_and_brief(tmp_path):
    request = {
        'prompt': 'Walk toward the plaza',
        'scene_context': {'standing_spots': [
            {'reference': f'Plaza:standing_{index}', 'position': [index, 0, 0]}
            for index in range(20)]},
        'manifest_summary': {'assets': [{'id': 'vehicle.car', 'anchors': ['door']}]},
        'director_brief_digest': 'A drawn route belongs to the car.',
        'function_catalog_schemas': [{'function': {'name': 'follow_path'}}],
    }
    output = tmp_path / 'pending_request.json'
    write_task_json(output, request)
    assert json.loads(output.read_text()) == request
    assert len(output.read_text().splitlines()) == 1
    assert output.stat().st_size < len(json.dumps(request, indent=2).encode())


def test_queue_rules_omit_only_inventories_supplied_as_fields():
    scene = {'objects': [{'name': 'Plaza', 'asset_id': 'environment.plaza',
                          'position': [0, 0, 0]}],
             'standing_spots': [{'owner': 'Plaza', 'name': 'standing_1',
                                 'position': [1, 0, 0]}]}
    manifest = {'assets': [{'id': 'cars.example', 'tags': ['vehicle'],
                            'anchors': {'door': {}}, 'surfaces': {}}]}
    planner = LLMShotPlanner(FunctionCatalog(manifest, scene_data=scene))
    full = planner.build_system_prompt()
    queue = planner.build_system_prompt(include_scene_inventory=False,
                                        include_brief_digest=False)
    assert 'Plaza:standing_1' in full and 'Plaza:standing_1' not in queue
    assert 'cars.example' in full and 'cars.example' not in queue
    assert 'follow_path' in queue and 'north = -Z' in queue
    assert 'Plan the whole shot as interactions' in queue
    assert "target the actor ID, not that actor's starting seat" in queue
    tools = {item['function']['name']: item['function'] for item in planner.export_tool_schemas()}
    assert 'actor targets resolve at arrival' in tools['run_to']['description']
